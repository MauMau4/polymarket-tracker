"""
Symptom-based data-freshness watchdog — complements the Gamma contract canary
(`run_gamma_canary.py`, which checks the API contract itself) by checking
whether OUR OWN pipeline is still actually producing output. The canary can
be green while the ingestor process is dead, discovery has silently stopped,
or the resolver is stuck — these symptoms are visible only from our own data,
not from the Gamma API contract.

Checks, all read-only:
  1. Trade silence: no trade ingested in `_TRADE_SILENCE_HOURS` — the
     ingestor process (whale/watched-wallet alerts) has likely died.
  2. Discovery silence: no market row touched (`updated_at`) in
     `_DISCOVERY_SILENCE_HOURS` — the discovery job has stopped running.
  3. Resolver zero-progress: unresolved markets already past their end_date
     exist (real work is waiting) but `resolution_checked_at` hasn't moved
     in `_RESOLVER_SILENCE_HOURS` — the resolver has stalled with a backlog,
     the exact failure mode found and fixed 2026-07-17 (decisions/2026-07-17
     .md, "Resolver sweep track wasn't committing on empty batches").

CRIT (logger.critical) on any failure, same convention as the Gamma canary.

Schedule: hourly via APScheduler (run_worker.py).
One-time run: python -m app.tasks.run_freshness_watchdog
"""
import asyncio
import sys
from datetime import datetime, timezone

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Market, Trade
from app.db.session import get_session_factory
from app.logging import setup_logging, get_logger

logger = get_logger(__name__)

_TRADE_SILENCE_HOURS = 2
_DISCOVERY_SILENCE_HOURS = 24
_RESOLVER_SILENCE_HOURS = 2


async def _compute_freshness(session: AsyncSession) -> dict:
    """Pure computation over an injected session — kept separate from
    run_freshness_watchdog() so tests can pass a session pointed at
    polymarket_test instead of going through get_session_factory(), which
    resolves DATABASE_URL from .env (production)."""
    now = datetime.now(tz=timezone.utc)
    problems: list[str] = []

    last_trade_ts = (await session.execute(select(func.max(Trade.ts)))).scalar_one_or_none()
    trade_silent_hours = (now - last_trade_ts).total_seconds() / 3600 if last_trade_ts else None
    if last_trade_ts is None:
        problems.append("trades table is completely empty")
    elif trade_silent_hours >= _TRADE_SILENCE_HOURS:
        problems.append(
            f"no trades ingested in {trade_silent_hours:.1f}h (threshold {_TRADE_SILENCE_HOURS}h) — "
            "ingestor process may be dead"
        )

    last_market_update = (await session.execute(select(func.max(Market.updated_at)))).scalar_one_or_none()
    discovery_silent_hours = (
        (now - last_market_update).total_seconds() / 3600 if last_market_update else None
    )
    if last_market_update is None:
        problems.append("markets table is completely empty")
    elif discovery_silent_hours >= _DISCOVERY_SILENCE_HOURS:
        problems.append(
            f"no discovery updates in {discovery_silent_hours:.1f}h (threshold {_DISCOVERY_SILENCE_HOURS}h) — "
            "discovery job may have stopped running"
        )

    resolver_candidates = (
        await session.execute(
            select(func.count()).select_from(Market).where(
                Market.resolved == False, Market.active == True,  # noqa: E712
                Market.end_date.is_not(None), Market.end_date < now,
            )
        )
    ).scalar_one()
    last_checked = (
        await session.execute(select(func.max(Market.resolution_checked_at)))
    ).scalar_one_or_none()
    resolver_silent_hours = (now - last_checked).total_seconds() / 3600 if last_checked else None
    if resolver_candidates > 0:
        if last_checked is None:
            problems.append(
                f"resolver has never checked any market, {resolver_candidates} candidates waiting"
            )
        elif resolver_silent_hours >= _RESOLVER_SILENCE_HOURS:
            problems.append(
                f"resolver made no progress in {resolver_silent_hours:.1f}h "
                f"(threshold {_RESOLVER_SILENCE_HOURS}h) with {resolver_candidates} candidates waiting"
            )

    result = {
        "checked_at": now.isoformat(),
        "trade_silent_hours": round(trade_silent_hours, 2) if trade_silent_hours is not None else None,
        "discovery_silent_hours": round(discovery_silent_hours, 2) if discovery_silent_hours is not None else None,
        "resolver_silent_hours": round(resolver_silent_hours, 2) if resolver_silent_hours is not None else None,
        "resolver_candidates_waiting": resolver_candidates,
        "problems": problems,
        "ok": not problems,
    }
    return result


async def run_freshness_watchdog() -> dict:
    """Opens its own session against DATABASE_URL, runs the checks, and logs
    CRIT/INFO. Never raises."""
    factory = get_session_factory()
    async with factory() as session:
        result = await _compute_freshness(session)

    log_fields = {k: v for k, v in result.items() if k not in ("problems", "ok")}
    if result["problems"]:
        logger.critical("freshness_watchdog_alert", problems=result["problems"], **log_fields)
        from app.services.alerts.system_alerts import send_system_alert
        await send_system_alert("CRIT", "freshness_watchdog", "; ".join(result["problems"]))
    else:
        logger.info("freshness_watchdog_ok", **log_fields)

    return result


async def main() -> None:
    setup_logging()
    print(f"=== Freshness Watchdog — {datetime.now(tz=timezone.utc).isoformat()} ===\n")
    result = await run_freshness_watchdog()
    print(f"Trade silence     : {result['trade_silent_hours']}h")
    print(f"Discovery silence : {result['discovery_silent_hours']}h")
    print(f"Resolver silence  : {result['resolver_silent_hours']}h ({result['resolver_candidates_waiting']} candidates waiting)")
    print(f"Problems          : {result['problems'] or 'none'}")
    print(f"Overall           : {'OK' if result['ok'] else 'ALERT (see CRIT log lines above)'}")


if __name__ == "__main__":
    if sys.platform == "win32":
        asyncio.run(main(), loop_factory=asyncio.SelectorEventLoop)
    else:
        asyncio.run(main())
