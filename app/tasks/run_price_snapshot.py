"""
Price snapshot capture for favorite-longshot bias analysis.

Captures Yes/No prices for markets approaching their close date where
one side is trading at >= 0.80 (a heavy favourite). Writes one T-48
snapshot and one T-24 snapshot per market.

Only markets with volume >= 10000 are eligible — this filters out
daily sports spread/O/U markets that would pollute the analytics.

Scheduled jobs (registered in run_worker.py):
  price_snapshot_48h  every 6 hours  → take_snapshot(48)
  price_snapshot_24h  every 3 hours  → take_snapshot(24)

Manual run:
  python -m app.tasks.run_price_snapshot [48|24]
"""
import asyncio
import sys
from datetime import datetime, timedelta, timezone

from sqlalchemy import delete, or_, select, update as sqla_update
from sqlalchemy.exc import IntegrityError

from app.db.models.market import Market
from app.db.models.market_price_snapshot import MarketPriceSnapshot
from app.db.session import get_session_factory
from app.logging import setup_logging, get_logger
from app.services.polymarket.gamma_client import fetch_market_prices_batch

logger = get_logger(__name__)

_HIGH_SIDE_THRESHOLD = 0.80
_MIN_VOLUME = 10_000
_BATCH_SIZE = 50

# ILIKE patterns for player-prop markets to exclude from snapshot capture.
# These are low-quality daily sports markets (O/U lines, assists, rebounds, spreads)
# that would pollute the favorite-longshot bias analytics.
_PROP_EXCLUSIONS = [
    "%O/U%",
    "% Spread%",
    "%Assists%",
    "%Rebounds%",
    "%over/under%",
]


async def take_snapshot(hours_window: int) -> dict:
    """
    Capture price snapshots for markets closing within hours_window hours.

    Only writes a row if the highest-priced outcome is >= 0.80 (any outcome
    labels, not just Yes/No). Skips if a snapshot for (market_id, bucket)
    already exists. Also refreshes markets.volume from the price response.

    Returns dict with counts: found, eligible, written, skipped, errors.
    """
    now = datetime.now(tz=timezone.utc)
    window_end = now + timedelta(hours=hours_window)

    factory = get_session_factory()

    async with factory() as session:
        result = await session.execute(
            select(
                Market.market_id,
                Market.end_date,
            )
            .where(
                Market.resolved == False,  # noqa: E712
                Market.active == True,     # noqa: E712
                Market.end_date.is_not(None),
                Market.end_date > now,
                Market.end_date <= window_end,
                # NULL volume = not yet fetched; include it and filter below
                # using the live volume returned alongside the prices.
                or_(Market.volume >= _MIN_VOLUME, Market.volume.is_(None)),
                # Exclude player-prop market patterns
                ~or_(*[Market.question.ilike(pat) for pat in _PROP_EXCLUSIONS]),
            )
            .order_by(Market.end_date.asc())
        )
        candidates = result.all()

    logger.info("price_snapshot_candidates", hours_window=hours_window, count=len(candidates))

    if not candidates:
        return {"found": 0, "eligible": 0, "written": 0, "skipped": 0, "errors": 0}

    found = len(candidates)
    eligible = 0
    written = 0
    skipped = 0
    errors = 0

    # Process in batches to avoid hammering the API
    for batch_start in range(0, len(candidates), _BATCH_SIZE):
        batch = candidates[batch_start: batch_start + _BATCH_SIZE]
        market_ids = [row.market_id for row in batch]

        fetched_volumes: dict[str, float] = {}
        try:
            prices = await fetch_market_prices_batch(market_ids, out_volumes=fetched_volumes)
        except Exception as exc:
            logger.error("price_snapshot_batch_fetch_error", error=str(exc))
            errors += len(batch)
            continue

        # Persist live volumes so the candidate filter works next run
        if fetched_volumes:
            async with factory() as session:
                for mid, vol in fetched_volumes.items():
                    await session.execute(
                        sqla_update(Market)
                        .where(Market.market_id == mid)
                        .values(volume=vol)
                        .execution_options(synchronize_session=False)
                    )
                await session.commit()

        for row in batch:
            try:
                # Enforce the volume floor with the live number (candidates
                # with NULL stored volume were included optimistically)
                live_vol = fetched_volumes.get(row.market_id)
                if live_vol is not None and live_vol < _MIN_VOLUME:
                    continue

                market_prices = prices.get(row.market_id)
                if not market_prices or len(market_prices) < 2:
                    continue

                # Generic two-outcome handling: high side = highest-priced
                # outcome label (works for Yes/No AND team-name/categorical
                # markets, which the old Yes/No-key lookup silently dropped).
                ranked = sorted(market_prices.items(), key=lambda kv: kv[1], reverse=True)
                high_side, high_side_price = ranked[0]
                other_price = ranked[1][1]

                # Only capture if the favourite is at/above the threshold
                if high_side_price < _HIGH_SIDE_THRESHOLD:
                    continue

                eligible += 1

                # yes_price/no_price hold the literal Yes/No prices when those
                # labels exist; otherwise (favourite price, other price).
                if "Yes" in market_prices and "No" in market_prices:
                    yes_price = float(market_prices["Yes"])
                    no_price = float(market_prices["No"])
                else:
                    yes_price = high_side_price
                    no_price = other_price

                # Compute hours to close and bucket
                end_dt = row.end_date
                if end_dt.tzinfo is None:
                    end_dt = end_dt.replace(tzinfo=timezone.utc)
                hours_to_close = (end_dt - now).total_seconds() / 3600.0
                snapshot_bucket = "T24" if hours_to_close <= 24.0 else "T48"

                snapshot = MarketPriceSnapshot(
                    market_id=row.market_id,
                    snapshot_ts=now,
                    hours_to_close=hours_to_close,
                    snapshot_bucket=snapshot_bucket,
                    yes_price=yes_price,
                    no_price=no_price,
                    high_side=high_side,
                    high_side_price=high_side_price,
                    resolved=False,
                    resolution=None,
                    high_side_won=None,
                )

                async with factory() as session:
                    try:
                        session.add(snapshot)
                        await session.commit()
                        written += 1
                        logger.debug(
                            "price_snapshot_written",
                            market_id=row.market_id,
                            bucket=snapshot_bucket,
                            high_side=high_side,
                            high_side_price=high_side_price,
                            hours_to_close=round(hours_to_close, 1),
                        )
                    except IntegrityError:
                        await session.rollback()
                        skipped += 1
                        logger.debug(
                            "price_snapshot_skipped_duplicate",
                            market_id=row.market_id,
                            bucket=snapshot_bucket,
                        )

            except Exception as exc:
                logger.error(
                    "price_snapshot_market_error",
                    market_id=row.market_id,
                    error=str(exc),
                )
                errors += 1

    logger.info(
        "price_snapshot_complete",
        hours_window=hours_window,
        found=found,
        eligible=eligible,
        written=written,
        skipped=skipped,
        errors=errors,
    )
    return {
        "found": found,
        "eligible": eligible,
        "written": written,
        "skipped": skipped,
        "errors": errors,
    }


async def cleanup_low_volume_snapshots() -> int:
    """
    Delete unresolved snapshot rows for markets with volume < 10000 or NULL volume.
    Resolved snapshots are always preserved regardless of market volume.
    Returns the number of rows deleted.
    """
    factory = get_session_factory()
    async with factory() as session:
        low_vol_market_ids = select(Market.market_id).where(
            (Market.volume < _MIN_VOLUME) | (Market.volume.is_(None))
        )
        result = await session.execute(
            delete(MarketPriceSnapshot).where(
                MarketPriceSnapshot.market_id.in_(low_vol_market_ids),
                MarketPriceSnapshot.resolved == False,  # noqa: E712 — never delete resolved rows
            )
        )
        await session.commit()
    deleted = result.rowcount
    logger.info("snapshot_low_volume_cleanup", deleted=deleted, min_volume=_MIN_VOLUME)
    return deleted


async def main() -> None:
    setup_logging()
    hours = int(sys.argv[1]) if len(sys.argv) > 1 else 48
    started = datetime.now(tz=timezone.utc)
    print(f"=== Price Snapshot (T-{hours}h) — started {started.strftime('%Y-%m-%d %H:%M:%S UTC')} ===\n")

    print("Cleaning up low-volume snapshots (volume < 10000)...")
    deleted = await cleanup_low_volume_snapshots()
    print(f"  Deleted {deleted} low-volume snapshot row(s).\n")

    result = await take_snapshot(hours)

    print("\n=== Summary ===")
    print(f"  Markets in window : {result['found']}")
    print(f"  Eligible (>=0.80) : {result['eligible']}")
    print(f"  Written           : {result['written']}")
    print(f"  Skipped (dup)     : {result['skipped']}")
    print(f"  Errors            : {result['errors']}")
    elapsed = (datetime.now(tz=timezone.utc) - started).total_seconds()
    print(f"  Elapsed           : {elapsed:.1f}s")


if __name__ == "__main__":
    if sys.platform == "win32":
        asyncio.run(main(), loop_factory=asyncio.SelectorEventLoop)
    else:
        asyncio.run(main())
