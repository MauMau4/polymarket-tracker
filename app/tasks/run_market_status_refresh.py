"""
Market status refresh — runs daily.

Finds markets in our DB that are marked active but have end_date in the past,
verifies each against the Gamma API, and updates closed/resolved status.
This prevents stale "ACTIVE" markets from polluting the Markets tab.
"""
import asyncio
import sys
from datetime import datetime, timezone

from sqlalchemy import select

from app.db.models.market import Market
from app.db.session import get_session_factory
from app.logging import setup_logging, get_logger

logger = get_logger(__name__)

_BATCH_LIMIT = 100


async def refresh_market_statuses() -> dict:
    from app.services.polymarket.gamma_client import fetch_market_by_id

    factory = get_session_factory()
    async with factory() as session:
        now = datetime.now(tz=timezone.utc)
        result = await session.execute(
            select(Market.market_id)
            .where(
                Market.active == True,  # noqa: E712
                Market.resolved == False,  # noqa: E712
                Market.end_date < now,
            )
            .order_by(Market.end_date.asc())
            .limit(_BATCH_LIMIT)
        )
        stale_ids = [row[0] for row in result.all()]

    if not stale_ids:
        logger.info("market_status_refresh_no_stale_markets")
        return {"checked": 0, "closed": 0, "resolved": 0, "unchanged": 0}

    logger.info("market_status_refresh_starting", stale_count=len(stale_ids))
    closed_count = 0
    resolved_count = 0
    unchanged_count = 0

    for market_id in stale_ids:
        try:
            info = await fetch_market_by_id(market_id)
            if info is None:
                unchanged_count += 1
                continue

            factory = get_session_factory()
            async with factory() as session:
                result = await session.execute(
                    select(Market).where(Market.market_id == market_id)
                )
                market = result.scalar_one_or_none()
                if market is None:
                    continue

                now = datetime.now(tz=timezone.utc)
                changed = False

                if info.resolved and info.resolution:
                    market.resolved = True
                    market.resolution = info.resolution
                    market.active = False
                    market.closed = True
                    market.updated_at = now
                    changed = True
                    resolved_count += 1
                    logger.info(
                        "market_status_marked_resolved",
                        market_id=market_id,
                        resolution=info.resolution,
                    )
                elif not info.active or info.closed:
                    market.active = False
                    market.closed = True
                    market.updated_at = now
                    changed = True
                    closed_count += 1
                    logger.info("market_status_marked_closed", market_id=market_id)
                else:
                    # Gamma still shows it as active — end_date may have been extended
                    unchanged_count += 1

                if changed:
                    await session.commit()

        except Exception as exc:
            logger.error("market_status_refresh_error", market_id=market_id, error=str(exc))
            unchanged_count += 1

    summary = {
        "checked": len(stale_ids),
        "closed": closed_count,
        "resolved": resolved_count,
        "unchanged": unchanged_count,
    }
    logger.info("market_status_refresh_complete", **summary)
    return summary


async def main() -> None:
    setup_logging()
    await refresh_market_statuses()


if __name__ == "__main__":
    if sys.platform == "win32":
        asyncio.run(main(), loop_factory=asyncio.SelectorEventLoop)
    else:
        asyncio.run(main())
