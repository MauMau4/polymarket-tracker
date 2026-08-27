"""
Interim data-continuity audit for the Pathfinder broad-universe accumulation
window (the ≥60-day span the 2026-09-17+ one-shot re-test depends on —
decisions/2026-07-19.md, "dormant-and-accumulating"). READ-ONLY.

Why this exists, separate from run_freshness_watchdog.py: the freshness
watchdog CRITs when trades go silent *right now* (live outage in progress),
then goes quiet again the moment ingestion resumes. It does not tell you that
a gap already happened and left a hole — e.g. the ~22.5h db/redis outage on
2026-07-22 (decisions/2026-07-23.md). For a re-test that needs a *continuous*
window, an already-recovered gap is exactly the thing that must not go
unnoticed. This job is the historical/complementary view: it audits the whole
window since subscription activation and reports per-day capture, coverage %,
and every gap — so the re-test starts on data known to be clean rather than
discovering a hole on day one.

Reads the `trades_full` view (trades UNION ALL trades_archive, migration 0028)
so continuity is measured across the 30-day retention purge, not just the hot
`trades` table.

Checks (all read-only):
  1. Window coverage: hours-with-trades / hours-expected since the anchor.
  2. Per-day capture: daily trade counts, zero-days, and low-water days
     (< _DAY_LOW_WATER_RATIO of the window's median day).
  3. Gap list: every run of consecutive trade-free hours >= _GAP_MIN_HOURS.
  4. Recent-continuity verdict: the largest gap inside the trailing
     _RECENT_WINDOW_HOURS — this is what drives the optional WARN alert, so
     already-logged historical gaps (e.g. 07-22) don't re-fire every run.
  5. Genre mix (trailing 7d): confirms the "broad universe" claim
     (sports + crypto + politics/elections) is actually present in the stream.

Anchor default: 2026-07-19 (subscription activation, the correct clock anchor
per decisions/2026-07-19.md — not metadata-only discovery activation).

Run (read-only, safe):
  python -m app.tasks.run_continuity_check
  python -m app.tasks.run_continuity_check --anchor 2026-07-19
  python -m app.tasks.run_continuity_check --alert     # also queue a WARN digest entry if a recent gap is found
"""
import argparse
import asyncio
import sys
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.session import get_session_factory
from app.logging import setup_logging, get_logger

logger = get_logger(__name__)

_DEFAULT_ANCHOR = date(2026, 7, 19)   # subscription activation
_GAP_MIN_HOURS = 2                    # matches the freshness watchdog's trade-silence threshold
_RECENT_WINDOW_HOURS = 48            # only gaps ending inside this window drive the alert
_DAY_LOW_WATER_RATIO = 0.40          # a day under 40% of the window median is flagged partial


def _floor_hour(dt: datetime) -> datetime:
    """Naive UTC hour floor (matches date_trunc('hour', ts AT TIME ZONE 'UTC'))."""
    dt = dt.astimezone(timezone.utc).replace(tzinfo=None)
    return dt.replace(minute=0, second=0, microsecond=0)


def _find_gaps(present_hours: set[datetime], start: datetime, end: datetime) -> list[dict]:
    """Walk [start, end) hour-by-hour; group consecutive absent hours into gaps."""
    gaps: list[dict] = []
    run_start: datetime | None = None
    h = start
    while h < end:
        if h not in present_hours:
            if run_start is None:
                run_start = h
        else:
            if run_start is not None:
                gaps.append({"start": run_start, "end": h, "hours": int((h - run_start).total_seconds() // 3600)})
                run_start = None
        h += timedelta(hours=1)
    if run_start is not None:
        gaps.append({"start": run_start, "end": end, "hours": int((end - run_start).total_seconds() // 3600)})
    return gaps


async def _compute_continuity(session: AsyncSession, anchor: date, now: datetime) -> dict:
    """Pure computation over an injected session — kept separate from
    run_continuity_check() so tests can pass a session pointed at
    polymarket_test instead of resolving DATABASE_URL (production)."""
    anchor_ts = datetime(anchor.year, anchor.month, anchor.day, tzinfo=timezone.utc)

    bounds = (await session.execute(
        text("SELECT min(ts) AS mn, max(ts) AS mx, count(*) AS n FROM trades_full WHERE ts >= :a"),
        {"a": anchor_ts},
    )).one()

    if bounds.n == 0:
        return {
            "anchor": anchor.isoformat(), "checked_at": now.isoformat(),
            "total_trades": 0, "first_ts": None, "last_ts": None,
            "coverage_pct": None, "expected_hours": None, "present_hours": 0,
            "daily": [], "zero_days": [], "low_water_days": [],
            "gaps": [], "recent_gap_hours": 0, "genre_7d": [],
            "problems": ["trades_full has no rows at or after the anchor — no accumulation to audit"],
            "ok": False,
        }

    hourly = (await session.execute(
        text(
            "SELECT date_trunc('hour', ts AT TIME ZONE 'UTC') AS hr, count(*) AS n "
            "FROM trades_full WHERE ts >= :a GROUP BY 1"
        ),
        {"a": anchor_ts},
    )).all()
    present_hours = {r.hr for r in hourly}

    start_hour = _floor_hour(anchor_ts)
    end_hour = _floor_hour(now)  # exclude the current partial hour
    expected_hours = max(int((end_hour - start_hour).total_seconds() // 3600), 0)
    present_in_window = sum(1 for h in present_hours if start_hour <= h < end_hour)
    coverage_pct = round(100.0 * present_in_window / expected_hours, 2) if expected_hours else None

    gaps = [g for g in _find_gaps(present_hours, start_hour, end_hour) if g["hours"] >= _GAP_MIN_HOURS]

    recent_cutoff = end_hour - timedelta(hours=_RECENT_WINDOW_HOURS)
    recent_gap_hours = max((g["hours"] for g in gaps if g["end"] > recent_cutoff), default=0)

    daily_counts: dict[date, int] = defaultdict(int)
    for r in hourly:
        daily_counts[r.hr.date()] += r.n
    all_days = [anchor + timedelta(days=i) for i in range((now.date() - anchor).days + 1)]
    daily = [{"day": d.isoformat(), "count": daily_counts.get(d, 0)} for d in all_days]
    # median over full days only (exclude today's partial and the anchor's partial)
    full_days = [daily_counts.get(d, 0) for d in all_days[1:-1]] if len(all_days) > 2 else []
    nonzero = sorted(c for c in full_days if c > 0)
    median = nonzero[len(nonzero) // 2] if nonzero else 0
    zero_days = [d["day"] for d in daily if d["count"] == 0 and d["day"] != now.date().isoformat()]
    low_water = (
        [d["day"] for d in daily[1:-1] if 0 < d["count"] < _DAY_LOW_WATER_RATIO * median]
        if median else []
    )

    genre_7d = (await session.execute(
        text(
            "SELECT COALESCE(m.category, '(uncategorized)') AS cat, count(*) AS n "
            "FROM trades_full t LEFT JOIN markets m ON m.market_id = t.market_id "
            "WHERE t.ts >= :c GROUP BY 1 ORDER BY 2 DESC"
        ),
        {"c": now - timedelta(days=7)},
    )).all()

    problems: list[str] = []
    last_silent_h = (now - bounds.mx).total_seconds() / 3600
    if last_silent_h >= _GAP_MIN_HOURS:
        problems.append(f"no trades in the last {last_silent_h:.1f}h — a live outage may be in progress right now")
    if recent_gap_hours >= _GAP_MIN_HOURS:
        problems.append(
            f"a {recent_gap_hours}h capture gap occurred within the trailing {_RECENT_WINDOW_HOURS}h "
            "(already recovered, but it punched a hole in the accumulation window)"
        )

    return {
        "anchor": anchor.isoformat(),
        "checked_at": now.isoformat(),
        "total_trades": bounds.n,
        "first_ts": bounds.mn.isoformat() if bounds.mn else None,
        "last_ts": bounds.mx.isoformat() if bounds.mx else None,
        "coverage_pct": coverage_pct,
        "expected_hours": expected_hours,
        "present_hours": present_in_window,
        "median_day": median,
        "daily": daily,
        "zero_days": zero_days,
        "low_water_days": low_water,
        "gaps": [{"start": g["start"].isoformat(), "end": g["end"].isoformat(), "hours": g["hours"]} for g in gaps],
        "recent_gap_hours": recent_gap_hours,
        "genre_7d": [{"category": r.cat, "trades": r.n} for r in genre_7d],
        "problems": problems,
        "ok": not problems,
    }


async def run_continuity_check(anchor: date = _DEFAULT_ANCHOR, alert: bool = False) -> dict:
    """Opens its own session against DATABASE_URL, runs the audit, logs, and
    optionally queues a WARN digest entry for a recent gap. Never raises the
    audit's own result away — but does surface infra failures as a CRIT so this
    monitor can't itself fail silently (the lesson of the 07-22 outage)."""
    try:
        factory = get_session_factory()
        async with factory() as session:
            result = await _compute_continuity(session, anchor, datetime.now(tz=timezone.utc))
    except Exception as exc:
        logger.critical("continuity_check_failed", error=str(exc), exc_info=True)
        if alert:
            from app.services.alerts.system_alerts import send_system_alert
            await send_system_alert("CRIT", "continuity_check", f"continuity audit could not run: {exc}")
        raise

    log_fields = {k: result[k] for k in ("total_trades", "coverage_pct", "recent_gap_hours", "zero_days")}
    if result["problems"]:
        logger.warning("continuity_check_alert", problems=result["problems"], **log_fields)
        if alert:
            from app.services.alerts.system_alerts import send_system_alert
            await send_system_alert("WARN", "continuity_check", "; ".join(result["problems"]))
    else:
        logger.info("continuity_check_ok", **log_fields)

    return result


def _format_report(r: dict) -> str:
    lines: list[str] = []
    lines.append(f"=== Pathfinder accumulation continuity — {r['checked_at']} ===")
    lines.append(f"Anchor (subscription activation): {r['anchor']}")
    lines.append("")
    if r["total_trades"] == 0:
        lines.append("No trades at or after the anchor — nothing to audit.")
        return "\n".join(lines)
    lines.append(f"Total trades in window : {r['total_trades']:,}")
    lines.append(f"First / last trade     : {r['first_ts']}  ->  {r['last_ts']}")
    lines.append(f"Hour coverage          : {r['present_hours']}/{r['expected_hours']} hours ({r['coverage_pct']}%)")
    lines.append(f"Median full day        : {r['median_day']:,} trades")
    lines.append("")
    lines.append("Daily capture:")
    for d in r["daily"]:
        flag = ""
        if d["day"] in r["zero_days"]:
            flag = "   <-- ZERO"
        elif d["day"] in r["low_water_days"]:
            flag = "   <-- low-water (partial capture)"
        lines.append(f"  {d['day']}  {d['count']:>8,}{flag}")
    lines.append("")
    if r["gaps"]:
        lines.append(f"Capture gaps (>= {_GAP_MIN_HOURS}h, full window):")
        for g in r["gaps"]:
            lines.append(f"  {g['start']}  ->  {g['end']}   ({g['hours']}h)")
    else:
        lines.append(f"Capture gaps (>= {_GAP_MIN_HOURS}h): none")
    lines.append("")
    lines.append("Genre mix, trailing 7d (broad-universe check):")
    for g in r["genre_7d"]:
        lines.append(f"  {g['category']:<22} {g['trades']:>8,}")
    lines.append("")
    if r["problems"]:
        lines.append("VERDICT: ATTENTION")
        for p in r["problems"]:
            lines.append(f"  - {p}")
    else:
        lines.append("VERDICT: OK — window is continuous over the last "
                     f"{_RECENT_WINDOW_HOURS}h; historical gaps (if any) listed above for the record.")
    return "\n".join(lines)


async def main() -> None:
    setup_logging()
    parser = argparse.ArgumentParser(description="Interim data-continuity audit (read-only).")
    parser.add_argument("--anchor", type=str, default=_DEFAULT_ANCHOR.isoformat(),
                        help="Window start (YYYY-MM-DD). Default: subscription activation 2026-07-19.")
    parser.add_argument("--alert", action="store_true",
                        help="Also queue a WARN digest entry (or CRIT on infra failure) if a recent gap is found.")
    args = parser.parse_args()
    anchor = datetime.strptime(args.anchor, "%Y-%m-%d").date()

    result = await run_continuity_check(anchor=anchor, alert=args.alert)
    print(_format_report(result))


if __name__ == "__main__":
    if sys.platform == "win32":
        asyncio.run(main(), loop_factory=asyncio.SelectorEventLoop)
    else:
        asyncio.run(main())
