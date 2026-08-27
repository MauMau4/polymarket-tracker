"""
Read-only projection report for the volume-ranked subscription coverage fix
(decisions/2026-07-18.md). Compares the current uncapped subscription set
against what `get_subscription_asset_ids()` would select, and estimates the
trades/day and DB-growth impact using real recent trade attribution — never
a guess. Makes no writes.

Run: python -m app.tasks.run_subscription_projection_report
"""
import asyncio
import sys
from datetime import datetime, timedelta, timezone

from sqlalchemy import select, func, text

from app.config import get_settings
from app.db.models import Market, Token
from app.db.session import get_session_factory
from app.logging import setup_logging, get_logger
from app.services.discovery.refresh import get_subscription_asset_ids, _MICRO_LIFECYCLE_SLUG_PATTERN

logger = get_logger(__name__)

_TRAILING_DAYS = 7
_BYTES_PER_TRADE_ROW = 1660  # measured 2026-07-17 (966MB / 611,749 rows), decisions/2026-07-17.md


async def run_projection_report() -> dict:
    factory = get_session_factory()
    settings = get_settings()

    async with factory() as session:
        # Current (uncapped) subscription set
        current_rows = (
            await session.execute(
                select(Token.asset_id, Token.market_id)
                .join(Market, Token.market_id == Market.market_id)
                .where(Market.active == True, Market.resolved == False, Market.closed == False)  # noqa: E712
            )
        ).all()
        current_asset_ids = {r[0] for r in current_rows}
        current_market_ids = {r[1] for r in current_rows}

        # New (volume-ranked + watched-augmented) subscription set
        new_asset_ids_list, diagnostics = await get_subscription_asset_ids(
            session, top_n=settings.subscription_top_n
        )
        new_asset_ids = set(new_asset_ids_list)

        new_market_rows = (
            await session.execute(
                select(Token.market_id).where(Token.asset_id.in_(new_asset_ids)).distinct()
            )
        ).scalars().all()
        new_market_ids = set(new_market_rows)

        # How many currently-subscribed markets are micro-lifecycle (Up/Down family)?
        all_active_slugs = (
            await session.execute(
                select(Market.market_id, Market.slug).where(
                    Market.active == True, Market.resolved == False, Market.closed == False  # noqa: E712
                )
            )
        ).all()
        micro_market_ids = {
            mid for mid, slug in all_active_slugs if _MICRO_LIFECYCLE_SLUG_PATTERN.search(slug or "")
        }

        dropped_market_ids = current_market_ids - new_market_ids
        added_market_ids = new_market_ids - current_market_ids

        # Real trade-attribution split over the trailing window: how many
        # recent trades landed in markets that WOULD be kept vs WOULD be
        # dropped under the new regime, and how many were in the micro-
        # lifecycle family specifically (these aren't in `markets` today only
        # if never discovered — but the ones that ARE discovered/subscribed
        # currently, if any, are measurable directly).
        since = datetime.now(tz=timezone.utc) - timedelta(days=_TRAILING_DAYS)
        trade_split = (
            await session.execute(
                text(
                    """
                    SELECT
                        CASE
                            WHEN market_id = ANY(:kept ::text[]) THEN 'kept'
                            WHEN market_id = ANY(:micro ::text[]) THEN 'micro_lifecycle'
                            ELSE 'dropped_other'
                        END AS bucket,
                        COUNT(*) AS n_trades
                    FROM trades_full
                    WHERE ts >= :since
                    GROUP BY 1
                    """
                ),
                {
                    "kept": list(new_market_ids),
                    "micro": list(micro_market_ids),
                    "since": since,
                },
            )
        ).all()
        trade_counts = {row[0]: row[1] for row in trade_split}

        total_recent_trades = sum(trade_counts.values())
        kept_trades = trade_counts.get("kept", 0)
        micro_trades = trade_counts.get("micro_lifecycle", 0)
        dropped_other_trades = trade_counts.get("dropped_other", 0)

    trades_per_day_current = total_recent_trades / _TRAILING_DAYS
    trades_per_day_projected = kept_trades / _TRAILING_DAYS

    result = {
        "current_market_count": len(current_market_ids),
        "current_asset_count": len(current_asset_ids),
        "new_market_count": len(new_market_ids),
        "new_asset_count": len(new_asset_ids),
        "dropped_market_count": len(dropped_market_ids),
        "added_market_count": len(added_market_ids),
        "diagnostics": diagnostics,
        "trailing_days": _TRAILING_DAYS,
        "total_recent_trades": total_recent_trades,
        "kept_trades": kept_trades,
        "micro_lifecycle_trades_in_currently_subscribed_set": micro_trades,
        "dropped_other_trades": dropped_other_trades,
        "trades_per_day_current": round(trades_per_day_current, 1),
        "trades_per_day_projected": round(trades_per_day_projected, 1),
        "projected_pct_of_current": (
            round(100 * trades_per_day_projected / trades_per_day_current, 1)
            if trades_per_day_current > 0 else None
        ),
        "projected_db_growth_mb_per_day": round(trades_per_day_projected * _BYTES_PER_TRADE_ROW / 1_000_000, 2),
        "current_db_growth_mb_per_day": round(trades_per_day_current * _BYTES_PER_TRADE_ROW / 1_000_000, 2),
    }
    return result


async def main() -> None:
    setup_logging()
    print(f"=== Subscription Projection Report — {datetime.now(tz=timezone.utc).isoformat()} ===\n")
    result = await run_projection_report()

    print("Market/asset counts:")
    print(f"  Current subscription : {result['current_market_count']} markets / {result['current_asset_count']} assets")
    print(f"  New (projected)      : {result['new_market_count']} markets / {result['new_asset_count']} assets")
    print(f"  Dropped               : {result['dropped_market_count']} markets")
    print(f"  Added (new discovery) : {result['added_market_count']} markets")
    print(f"  Diagnostics: {result['diagnostics']}")
    print()
    print(f"Trailing {result['trailing_days']}-day trade attribution (real data, not estimated):")
    print(f"  Total trades in window                         : {result['total_recent_trades']}")
    print(f"  ...in markets the new regime would KEEP         : {result['kept_trades']}")
    print(f"  ...in micro-lifecycle (Up/Down) markets today    : {result['micro_lifecycle_trades_in_currently_subscribed_set']}")
    print(f"  ...in other markets the new regime would DROP    : {result['dropped_other_trades']}")
    print()
    print(f"Trades/day current    : {result['trades_per_day_current']}")
    print(f"Trades/day projected  : {result['trades_per_day_projected']} ({result['projected_pct_of_current']}% of current)")
    print(f"DB growth/day current : {result['current_db_growth_mb_per_day']} MB")
    print(f"DB growth/day proj.   : {result['projected_db_growth_mb_per_day']} MB")


if __name__ == "__main__":
    if sys.platform == "win32":
        asyncio.run(main(), loop_factory=asyncio.SelectorEventLoop)
    else:
        asyncio.run(main())
