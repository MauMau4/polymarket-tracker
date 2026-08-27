"""
Data retention — daily purge of old data.

Trade purge conditions (ALL must be true):
  1. Trade is older than TRADE_RETENTION_DAYS (configurable, default 30)
  2. The trade's market is resolved (Market.resolved = True)
  3. Either the trade has no wallet (unattributed) OR the wallet's
     score_computed_at IS NOT NULL (stats have been computed for the wallet)

Trades in unresolved markets are NEVER purged regardless of age.

Purge is archive-then-delete (2026-07-17): eligible rows are copied into
trades_archive and the copy count is verified against the source count
BEFORE the delete runs, all in one transaction — a mismatch aborts the
whole purge rather than deleting anything. See decisions/2026-07-17.md and
app/tasks/run_seed_trades_archive_20260717.py for the one-time backfill of
pre-existing history. Pathfinder research code reads trades_full (trades
UNION ALL trades_archive); tracker hot paths keep reading trades only.

Score history purge: wallet_score_history rows older than 7 days.
"""
from datetime import datetime, timedelta, timezone

from sqlalchemy import delete, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.db.models.market import Market
from app.db.models.trade import Trade
from app.db.models.wallet import Wallet
from app.db.models.wallet_score import WalletScoreHistory
from app.db.session import get_session_factory
from app.logging import get_logger

logger = get_logger(__name__)

_ARCHIVE_COLUMNS = (
    "id, external_trade_id, ts, market_id, asset_id, outcome, price, size, "
    "notional_usd, side, wallet, tx_hash, attribution_status, source, "
    "raw_payload, trade_type, outcome_result, created_at"
)


async def ensure_archive_partition(session: AsyncSession, ts: datetime) -> None:
    """Idempotently create the monthly trades_archive partition covering `ts`.

    Migration 0028 pre-created partitions through 2026-09; this keeps the
    table self-sufficient beyond that without a recurring maintenance chore.
    """
    month_start = ts.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    next_month = (month_start + timedelta(days=32)).replace(day=1)
    suffix = month_start.strftime("%Y_%m")
    await session.execute(text(
        f"""
        CREATE TABLE IF NOT EXISTS trades_archive_{suffix}
        PARTITION OF trades_archive
        FOR VALUES FROM ('{month_start.date().isoformat()}') TO ('{next_month.date().isoformat()}')
        """
    ))


async def _archive_then_delete(session: AsyncSession, where_clause) -> int:
    """Archive-then-delete a Trade subset: INSERT ... SELECT into trades_archive
    (skipping rows already archived — e.g. by the one-time 2026-07-17 seed),
    verify every source row ends up archived, then DELETE — all in the
    caller's transaction. Raises RuntimeError (aborting the transaction) on
    any count mismatch rather than deleting anything.
    """
    select_stmt = select(Trade).where(*where_clause)
    source_rows = (await session.execute(select_stmt)).scalars().all()
    if not source_rows:
        return 0

    # Partitions are monthly; a purge batch can span multiple months.
    months_seen = {r.ts.replace(day=1, hour=0, minute=0, second=0, microsecond=0) for r in source_rows}
    for month_start in months_seen:
        await ensure_archive_partition(session, month_start)

    ids = [r.id for r in source_rows]

    # No id column in trades_archive, so re-running against rows the
    # one-time seed already copied must not duplicate them.
    already_archived = (await session.execute(text(
        "SELECT id FROM trades_archive WHERE id = ANY(:ids)"
    ), {"ids": ids})).scalars().all()
    to_insert = [i for i in ids if i not in set(already_archived)]

    inserted_count = 0
    if to_insert:
        insert_result = await session.execute(text(
            f"""
            INSERT INTO trades_archive ({_ARCHIVE_COLUMNS})
            SELECT {_ARCHIVE_COLUMNS} FROM trades WHERE id = ANY(:ids)
            RETURNING id
            """
        ), {"ids": to_insert})
        inserted_count = len(insert_result.all())

    total_archived = inserted_count + len(already_archived)
    if total_archived != len(source_rows):
        raise RuntimeError(
            f"trades_archive count mismatch: {len(source_rows)} source rows, "
            f"{inserted_count} newly archived + {len(already_archived)} already archived "
            f"= {total_archived} — aborting purge, nothing deleted"
        )

    delete_result = await session.execute(delete(Trade).where(Trade.id.in_(ids)))
    deleted_count = delete_result.rowcount or 0

    if deleted_count != len(source_rows):
        raise RuntimeError(
            f"trades delete count mismatch: expected {len(source_rows)}, deleted {deleted_count} "
            "— aborting purge"
        )

    return deleted_count


async def purge_old_trades() -> int:
    """
    Archive then delete eligible old trades; return count purged.

    Uses two passes to avoid large single DELETE:
      Pass 1 — unattributed trades in resolved markets older than cutoff
      Pass 2 — attributed trades in resolved markets older than cutoff
               where wallet.score_computed_at IS NOT NULL

    Each pass is archived and verified before its delete runs, in one
    transaction per pass — see _archive_then_delete.
    """
    settings = get_settings()
    cutoff = datetime.now(tz=timezone.utc) - timedelta(days=settings.trade_retention_days)

    resolved_market_ids_sq = (
        select(Market.market_id).where(Market.resolved == True).scalar_subquery()  # noqa: E712
    )
    scored_wallet_sq = (
        select(Wallet.wallet).where(Wallet.score_computed_at.is_not(None)).scalar_subquery()
    )

    factory = get_session_factory()

    async with factory() as session:
        purged1 = await _archive_then_delete(session, [
            Trade.ts < cutoff,
            Trade.market_id.in_(resolved_market_ids_sq),
            Trade.wallet.is_(None),
        ])
        await session.commit()

    async with factory() as session:
        purged2 = await _archive_then_delete(session, [
            Trade.ts < cutoff,
            Trade.market_id.in_(resolved_market_ids_sq),
            Trade.wallet.is_not(None),
            Trade.wallet.in_(scored_wallet_sq),
        ])
        await session.commit()

    total_purged = purged1 + purged2
    logger.info(
        "retention_purge_complete",
        cutoff_days=settings.trade_retention_days,
        unattributed_purged=purged1,
        attributed_purged=purged2,
        total_purged=total_purged,
    )
    return total_purged


_SCORE_HISTORY_RETENTION_DAYS = 7


async def purge_old_score_history() -> int:
    """Delete wallet_score_history rows older than 7 days. Returns count deleted."""
    cutoff = datetime.now(tz=timezone.utc) - timedelta(days=_SCORE_HISTORY_RETENTION_DAYS)
    factory = get_session_factory()
    async with factory() as session:
        result = await session.execute(
            delete(WalletScoreHistory).where(WalletScoreHistory.snapshot_ts < cutoff)
        )
        purged = result.rowcount or 0
        await session.commit()
    logger.info("score_history_purge_complete", cutoff_days=_SCORE_HISTORY_RETENTION_DAYS, purged=purged)
    return purged
