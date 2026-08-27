"""
One-time backfill: fetch Gamma API metadata for every market_id in trades
that has no corresponding row in the markets table, then run resolution sync
so newly-inserted markets get their resolved/closed status populated.

Usage:
    python -m app.tasks.run_market_backfill

Steps performed automatically:
  1. Find all distinct market_ids in trades with no markets row
  2. Fetch each from Gamma API (batched, rate-limit friendly)
  3. Upsert each result to the markets table
  4. Run the existing resolution sync job against the new rows
  5. Report final orphan count
"""
import asyncio
import sys
from datetime import datetime, timezone

from sqlalchemy import select, text

from app.db.models.market import Market
from app.db.models.trade import Trade
from app.db.session import get_session_factory
from app.logging import setup_logging, get_logger
from app.services.polymarket.gamma_client import fetch_market_by_id
from app.services.discovery.refresh import _upsert_market

logger = get_logger(__name__)

_BATCH_SIZE = 10          # markets per burst
_INTER_BATCH_SLEEP = 0.5  # seconds between bursts


async def _find_orphaned_market_ids() -> list[str]:
    """Return distinct market_ids in trades with no corresponding markets row."""
    factory = get_session_factory()
    async with factory() as session:
        rows = await session.execute(
            select(Trade.market_id)
            .outerjoin(Market, Trade.market_id == Market.market_id)
            .where(Market.market_id.is_(None))
            .where(Trade.market_id.isnot(None))
            .where(Trade.market_id != "")
            .distinct()
            .order_by(Trade.market_id)
        )
        return [r[0] for r in rows.all()]


async def _count_orphans() -> int:
    """Count distinct market_ids in trades with no markets row (correct join)."""
    factory = get_session_factory()
    async with factory() as session:
        result = await session.execute(text("""
            SELECT COUNT(DISTINCT t.market_id)
            FROM trades t
            LEFT JOIN markets m ON t.market_id = m.market_id
            WHERE m.market_id IS NULL
              AND t.market_id IS NOT NULL
              AND t.market_id != ''
        """))
        return result.scalar_one() or 0


async def run_backfill() -> dict:
    # ── Step 1: identify missing market_ids ──────────────────────────────────
    logger.info("backfill_step1_finding_orphans")
    missing_ids = await _find_orphaned_market_ids()
    total = len(missing_ids)
    logger.info("backfill_orphans_found", count=total)
    print(f"\n[Step 1] Found {total} market_ids in trades with no markets row.")

    if total == 0:
        print("Nothing to backfill.")
        return {"total_missing": 0, "backfilled": 0, "not_found": [], "errors": []}

    # ── Step 2 + 3: fetch from Gamma and upsert ──────────────────────────────
    print(f"[Step 2] Fetching from Gamma API in batches of {_BATCH_SIZE}…")
    factory = get_session_factory()
    backfilled = 0
    not_found: list[str] = []
    errors: list[str] = []

    for i, market_id in enumerate(missing_ids):
        try:
            info = await fetch_market_by_id(market_id)
            if info is None:
                not_found.append(market_id)
                logger.warning("backfill_market_not_found", market_id=market_id, n=i + 1, total=total)
                continue

            async with factory() as session:
                await _upsert_market(session, info)
                await session.commit()

            backfilled += 1
            if backfilled % 50 == 0 or backfilled == 1:
                logger.info("backfill_progress", backfilled=backfilled, total=total)
                print(f"  … {backfilled}/{total} upserted")

        except Exception as exc:
            errors.append(market_id)
            logger.error("backfill_market_error", market_id=market_id, error=str(exc))

        # Rate-limit: pause after each batch
        if (i + 1) % _BATCH_SIZE == 0:
            await asyncio.sleep(_INTER_BATCH_SLEEP)

    print(f"[Step 2/3] Done. Upserted: {backfilled}, not found in Gamma: {len(not_found)}, errors: {len(errors)}")
    logger.info(
        "backfill_complete",
        backfilled=backfilled,
        not_found=len(not_found),
        errors=len(errors),
    )
    if not_found:
        logger.warning("backfill_not_found_list", market_ids=not_found)
        print(f"  Market IDs Gamma could not resolve ({len(not_found)}):")
        for mid in not_found:
            print(f"    {mid}")
    if errors:
        print(f"  Market IDs that errored ({len(errors)}):")
        for mid in errors:
            print(f"    {mid}")

    # ── Step 3 (resolution sync) ─────────────────────────────────────────────
    print("\n[Step 3] Running resolution sync on newly inserted markets…")
    logger.info("backfill_step3_resolution_sync")
    try:
        from app.services.markets.resolution import check_and_process_resolved_markets
        summary = await check_and_process_resolved_markets()
        print(
            f"  Resolution sync: synced={summary['markets_synced']}, "
            f"processed={summary['markets_processed']}, "
            f"wallets_rescored={summary['wallets_rescored']}"
        )
        logger.info("backfill_resolution_sync_done", **summary)
    except Exception as exc:
        logger.error("backfill_resolution_sync_error", error=str(exc))
        print(f"  Resolution sync error: {exc}")

    # ── Step 4: final orphan count ────────────────────────────────────────────
    print("\n[Step 4] Verifying — counting remaining orphaned market_ids…")
    final_count = await _count_orphans()
    print(f"  Remaining orphans: {final_count}")
    logger.info("backfill_final_orphan_count", count=final_count)

    return {
        "total_missing": total,
        "backfilled": backfilled,
        "not_found": not_found,
        "errors": errors,
        "final_orphan_count": final_count,
    }


async def main() -> None:
    setup_logging()
    started = datetime.now(tz=timezone.utc)
    print(f"=== Market Backfill — started {started.strftime('%Y-%m-%d %H:%M:%S UTC')} ===\n")

    result = await run_backfill()

    elapsed = (datetime.now(tz=timezone.utc) - started).total_seconds()
    print(f"\n=== Summary ===")
    print(f"  Markets missing at start : {result['total_missing']}")
    print(f"  Upserted from Gamma      : {result['backfilled']}")
    print(f"  Not found in Gamma       : {len(result.get('not_found', []))}")
    print(f"  Errors                   : {len(result.get('errors', []))}")
    print(f"  Final orphan count       : {result.get('final_orphan_count', '?')}")
    print(f"  Elapsed                  : {elapsed:.1f}s")


if __name__ == "__main__":
    if sys.platform == "win32":
        asyncio.run(main(), loop_factory=asyncio.SelectorEventLoop)
    else:
        asyncio.run(main())
