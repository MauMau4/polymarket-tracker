"""Read-only per-phase coverage-discovery projection report
(decisions/2026-07-19.md item 4). Live census against Gamma for a phase's
genre tags, cross-referenced against what's already in `markets`, plus a
DB-growth estimate. Makes no writes — meant to be reviewed by the operator
BEFORE that phase's genres are added to settings.enabled_genre_tags_csv.

Trades/day here is explicitly a volume-proportional PROJECTION, not a
measurement: Polymarket exposes no trade-frequency field, and there is no
historical trade data for markets we've never subscribed to (the same
"never fabricate" concern raised in decisions/2026-07-18.md). The method
scales the currently-measured trades/day for the existing subscription by
the ratio of (new genre markets' summed lifetime volume) to (currently
active markets' summed lifetime volume) — both volume figures are real,
live-measured numbers; only the proportionality assumption is an estimate,
and it is labeled as such throughout, never presented as measured.

Run: python -m app.tasks.run_genre_discovery_projection politics elections
"""
from __future__ import annotations

import asyncio
import sys
from datetime import datetime, timedelta, timezone

from sqlalchemy import func, select, text

from app.config import get_settings
from app.db.models import Market
from app.db.session import get_session_factory
from app.logging import get_logger, setup_logging
from app.services.discovery.refresh import _MICRO_LIFECYCLE_SLUG_PATTERN
from app.services.polymarket.gamma_client import fetch_sports_markets_by_tag
from app.tasks.run_genre_discovery import GENRE_TAG_SLUGS

logger = get_logger(__name__)

_TRAILING_DAYS = 7


async def _measure_bytes_per_row(session, table: str) -> float:
    """Live total-relation-size / row-count for `table` — same method used to
    derive the frozen _BYTES_PER_TRADE_ROW constant in
    run_subscription_projection_report.py on 2026-07-17, computed fresh here
    instead of reusing a constant that goes stale as the table grows."""
    row = (
        await session.execute(
            text(f"SELECT pg_total_relation_size('{table}'), (SELECT count(*) FROM {table})")
        )
    ).one()
    total_bytes, row_count = row[0], row[1]
    if not row_count:
        return 0.0
    return total_bytes / row_count


async def _current_baseline(session) -> dict:
    """Real, measured current state: active-subscription volume and
    trailing-7d trades/day, for the volume-proportional projection below."""
    active_volume = (
        await session.execute(
            select(func.coalesce(func.sum(Market.volume), 0.0)).where(
                Market.active == True, Market.resolved == False, Market.closed == False  # noqa: E712
            )
        )
    ).scalar()

    since = datetime.now(tz=timezone.utc) - timedelta(days=_TRAILING_DAYS)
    total_recent_trades = (
        await session.execute(
            text("SELECT count(*) FROM trades_full WHERE ts >= :since"), {"since": since}
        )
    ).scalar()

    return {
        "active_subscribed_volume_usd": float(active_volume or 0.0),
        "trades_per_day_current": (total_recent_trades or 0) / _TRAILING_DAYS,
    }


async def _census_genre(genre: str) -> dict:
    """Live Gamma census for one genre's tag_slug(s): open markets found,
    total volume, and how many are already in our `markets` table."""
    tag_slugs = GENRE_TAG_SLUGS[genre]
    factory = get_session_factory()

    found_market_ids: set[str] = set()
    total_volume = 0.0
    excluded_micro = 0

    for tag_slug in tag_slugs:
        markets = await fetch_sports_markets_by_tag(tag_slug)
        for m in markets:
            if _MICRO_LIFECYCLE_SLUG_PATTERN.search(m.slug or ""):
                excluded_micro += 1
                continue
            found_market_ids.add(m.market_id)
            total_volume += m.volume or 0.0

    already_in_db = 0
    if found_market_ids:
        async with factory() as session:
            already_in_db = (
                await session.execute(
                    select(func.count(Market.market_id)).where(Market.market_id.in_(found_market_ids))
                )
            ).scalar()

    return {
        "genre": genre,
        "tag_slugs": tag_slugs,
        "markets_found": len(found_market_ids),
        "excluded_micro_lifecycle": excluded_micro,
        "total_volume_usd": total_volume,
        "already_in_db": already_in_db,
        "new_markets": len(found_market_ids) - already_in_db,
    }


async def run_phase_projection(genres: list[str]) -> dict:
    factory = get_session_factory()

    async with factory() as session:
        baseline = await _current_baseline(session)
        bytes_per_market_row = await _measure_bytes_per_row(session, "markets")
        bytes_per_token_row = await _measure_bytes_per_row(session, "tokens")
        bytes_per_trade_row = await _measure_bytes_per_row(session, "trades")

    per_genre = []
    for genre in genres:
        if genre not in GENRE_TAG_SLUGS:
            raise ValueError(f"unknown genre {genre!r}; valid: {sorted(GENRE_TAG_SLUGS)}")
        per_genre.append(await _census_genre(genre))

    total_new_markets = sum(g["new_markets"] for g in per_genre)
    total_new_volume = sum(g["total_volume_usd"] for g in per_genre)
    # Each market carries ~2 tokens (binary Yes/No) on average across this
    # codebase's existing markets — used only as a rough per-row multiplier
    # for the one-time DB-growth estimate, not claimed as exact.
    estimated_new_tokens = total_new_markets * 2

    active_volume = baseline["active_subscribed_volume_usd"]
    trades_per_day_current = baseline["trades_per_day_current"]
    volume_ratio = (total_new_volume / active_volume) if active_volume > 0 else None
    trades_per_day_projected = (
        trades_per_day_current * volume_ratio if volume_ratio is not None else None
    )

    one_time_growth_mb = (
        total_new_markets * bytes_per_market_row + estimated_new_tokens * bytes_per_token_row
    ) / 1_000_000
    ongoing_growth_mb_per_day = (
        (trades_per_day_projected * bytes_per_trade_row / 1_000_000)
        if trades_per_day_projected is not None
        else None
    )
    ongoing_growth_mb_per_month = (
        ongoing_growth_mb_per_day * 30 if ongoing_growth_mb_per_day is not None else None
    )

    return {
        "genres": genres,
        "per_genre": per_genre,
        "total_new_markets": total_new_markets,
        "estimated_new_tokens": estimated_new_tokens,
        "total_new_volume_usd": total_new_volume,
        "baseline_active_subscribed_volume_usd": active_volume,
        "baseline_trades_per_day_current": round(trades_per_day_current, 1),
        "volume_ratio_new_to_current": round(volume_ratio, 4) if volume_ratio is not None else None,
        "projected_trades_per_day": (
            round(trades_per_day_projected, 1) if trades_per_day_projected is not None else None
        ),
        "one_time_db_growth_mb": round(one_time_growth_mb, 2),
        "projected_ongoing_db_growth_mb_per_day": (
            round(ongoing_growth_mb_per_day, 2) if ongoing_growth_mb_per_day is not None else None
        ),
        "projected_ongoing_db_growth_mb_per_month": (
            round(ongoing_growth_mb_per_month, 2) if ongoing_growth_mb_per_month is not None else None
        ),
        "bytes_per_market_row": round(bytes_per_market_row, 1),
        "bytes_per_token_row": round(bytes_per_token_row, 1),
        "bytes_per_trade_row": round(bytes_per_trade_row, 1),
    }


async def main() -> None:
    setup_logging()
    genres = sys.argv[1:] or list(GENRE_TAG_SLUGS)
    print(f"=== Genre Discovery Projection Report — {datetime.now(tz=timezone.utc).isoformat()} ===")
    print(f"Genres: {genres}\n")

    result = await run_phase_projection(genres)

    print("Per-genre live census (Gamma, open markets, cross-referenced vs our DB):")
    for g in result["per_genre"]:
        print(
            f"  {g['genre']:<12} tag_slugs={g['tag_slugs']} found={g['markets_found']:>6} "
            f"already_in_db={g['already_in_db']:>6} new={g['new_markets']:>6} "
            f"volume=${g['total_volume_usd']:,.0f} excluded_micro={g['excluded_micro_lifecycle']}"
        )
    print()
    print(f"Total new markets to ingest : {result['total_new_markets']}")
    print(f"Estimated new tokens        : {result['estimated_new_tokens']} (~2/market, binary Yes/No)")
    print(f"Total new volume (lifetime) : ${result['total_new_volume_usd']:,.0f}")
    print()
    print("--- Volume-proportional PROJECTION (not measured) ---")
    print(f"Baseline active-subscribed volume : ${result['baseline_active_subscribed_volume_usd']:,.0f}")
    print(f"Baseline trades/day (measured)    : {result['baseline_trades_per_day_current']}")
    print(f"Volume ratio (new / current)      : {result['volume_ratio_new_to_current']}")
    print(f"Projected trades/day              : {result['projected_trades_per_day']}")
    print()
    print(f"One-time DB growth (markets+tokens rows) : {result['one_time_db_growth_mb']} MB")
    print(f"Projected ongoing DB growth/day           : {result['projected_ongoing_db_growth_mb_per_day']} MB")
    print(f"Projected ongoing DB growth/month         : {result['projected_ongoing_db_growth_mb_per_month']} MB")
    print()
    print(
        f"(measured bytes/row — markets: {result['bytes_per_market_row']}, "
        f"tokens: {result['bytes_per_token_row']}, trades: {result['bytes_per_trade_row']})"
    )


if __name__ == "__main__":
    if sys.platform == "win32":
        asyncio.run(main(), loop_factory=asyncio.SelectorEventLoop)
    else:
        asyncio.run(main())
