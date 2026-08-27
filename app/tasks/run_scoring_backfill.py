"""
Historical backfill: populate wallet_closed_positions from existing trade data.

IMPORTANT: Run this script BEFORE applying migration 0014 (which drops
skill_score, edge_score, exit_skill from trades). After 0014, the exit_skill
column no longer exists and the SELL backfill portion will be skipped.

Two sources:
  1. Resolved BUY trades — trades with outcome_result set (market resolved).
     entry_price = trade.price
     exit_price  = 1.0 (win) or 0.0 (loss) from outcome_result
     Each resolved BUY trade becomes one wallet_closed_positions row.

  2. Historical SELL trades — SELL trades that have exit_skill populated.
     entry_price = trade.price - trade.exit_skill
     exit_price  = trade.price
     exit_skill was set at ingestion: exit_skill = sell_price - avg_entry_price

After writing rows, triggers a full wallet score recomputation.

Usage:
    python -m app.tasks.run_scoring_backfill
"""
import asyncio
import math
import sys
from datetime import datetime, timezone

from sqlalchemy import select, func as sqlfunc, text

from app.db.models.market import Market
from app.db.models.token import Token
from app.db.models.trade import Trade
from app.db.models.wallet_closed_position import WalletClosedPosition
from app.db.session import get_session_factory
from app.logging import setup_logging, get_logger

logger = get_logger(__name__)


async def _get_wallet_lifetime_notional(session, wallet: str) -> float:
    row = await session.execute(
        select(sqlfunc.sum(Trade.notional_usd))
        .where(Trade.wallet == wallet, Trade.notional_usd.is_not(None), Trade.side == "BUY")
    )
    return float(row.scalar_one_or_none() or 0.0)


async def _already_backfilled_resolved(session, wallet: str, asset_id: str, market_id: str) -> bool:
    """True if a resolved closed position already exists for this (wallet, asset_id, market)."""
    row = await session.execute(
        select(sqlfunc.count())
        .select_from(WalletClosedPosition)
        .where(
            WalletClosedPosition.wallet == wallet,
            WalletClosedPosition.asset_id == asset_id,
            WalletClosedPosition.market_id == market_id,
            WalletClosedPosition.is_resolved == True,  # noqa: E712
        )
    )
    return row.scalar_one() > 0


async def backfill_resolved_buys(session) -> int:
    """
    Write wallet_closed_positions for all resolved BUY trades.
    Returns count of rows written.
    """
    rows_result = await session.execute(
        select(
            Trade.id,
            Trade.wallet,
            Trade.asset_id,
            Trade.market_id,
            Trade.price,
            Trade.size,
            Trade.outcome,
            Trade.outcome_result,
            Trade.ts,
        )
        .where(
            Trade.side == "BUY",
            Trade.outcome_result.is_not(None),
            Trade.wallet.is_not(None),
            Trade.price.is_not(None),
            Trade.size.is_not(None),
            Trade.trade_type.is_(None),
        )
    )
    trades = rows_result.all()

    if not trades:
        return 0

    # Batch-fetch true settlement times (never end_date — see docs/m1-schema-audit.md)
    market_ids = list({r.market_id for r in trades})
    mkt_result = await session.execute(
        select(Market.market_id, Market.resolved_at).where(Market.market_id.in_(market_ids))
    )
    market_resolved_ats = {r.market_id: r.resolved_at for r in mkt_result.all()}

    # Cache wallet lifetime notionals
    wallet_notionals: dict[str, float] = {}

    written = 0
    skipped_dup = 0
    skipped_math = 0

    for row in trades:
        entry_price = float(row.price)
        if entry_price <= 0:
            skipped_math += 1
            continue

        exit_price = 1.0 if row.outcome_result == 1 else 0.0
        shares = float(row.size)
        cost_basis = entry_price * shares
        realized_pnl_val = (exit_price - entry_price) * shares

        argument = max(1e-10, 1.0 + (exit_price - entry_price) / entry_price)
        log_roi_val = math.log(argument)

        # Skip if already written
        if await _already_backfilled_resolved(session, row.wallet, row.asset_id, row.market_id):
            skipped_dup += 1
            continue

        if row.wallet not in wallet_notionals:
            wallet_notionals[row.wallet] = await _get_wallet_lifetime_notional(session, row.wallet)
        lifetime_notional = wallet_notionals[row.wallet]
        conviction = cost_basis / lifetime_notional if lifetime_notional > 0 else 0.0

        resolved_at = market_resolved_ats.get(row.market_id)
        closed_at = resolved_at or row.ts
        closed_at_source = "gamma_settlement" if resolved_at is not None else "detection_time_proxy"

        session.add(WalletClosedPosition(
            wallet=row.wallet,
            asset_id=row.asset_id,
            market_id=row.market_id,
            closed_at_source=closed_at_source,
            entry_price=entry_price,
            exit_price=exit_price,
            shares_sold=shares,
            total_shares_at_time=shares,
            position_fraction=1.0,
            cost_basis=cost_basis,
            conviction_weight=conviction,
            log_roi=log_roi_val,
            realized_pnl=realized_pnl_val,
            is_resolved=True,
            closed_at=closed_at,
        ))
        written += 1

    logger.info(
        "backfill_resolved_buys",
        written=written,
        skipped_dup=skipped_dup,
        skipped_math=skipped_math,
    )
    return written


async def backfill_sell_exits(session) -> int:
    """
    Write wallet_closed_positions for historical SELL trades that have exit_skill.
    Uses raw SQL to read exit_skill since the ORM column has been removed.
    If the column doesn't exist (migration 0014 already ran), returns 0.
    """
    # Check if exit_skill column still exists before querying
    try:
        check_result = await session.execute(text("""
            SELECT COUNT(*) FROM information_schema.columns
            WHERE table_name = 'trades' AND column_name = 'exit_skill'
        """))
        if check_result.scalar_one() == 0:
            logger.info("backfill_sell_exits_skipped_column_missing")
            return 0
    except Exception:
        return 0

    rows_result = await session.execute(text("""
        SELECT id, wallet, asset_id, market_id, price, size, ts, exit_skill
        FROM trades
        WHERE side = 'SELL'
          AND exit_skill IS NOT NULL
          AND wallet IS NOT NULL
          AND price IS NOT NULL
          AND size IS NOT NULL
          AND trade_type IS NULL
    """))
    trades = rows_result.all()

    if not trades:
        return 0

    wallet_notionals: dict[str, float] = {}
    written = 0
    skipped_math = 0

    for row in trades:
        sell_price = float(row.price)
        exit_skill_val = float(row.exit_skill)
        entry_price = sell_price - exit_skill_val

        if entry_price <= 0:
            skipped_math += 1
            continue

        shares = float(row.size)
        cost_basis = entry_price * shares
        realized_pnl_val = (sell_price - entry_price) * shares

        try:
            log_roi_val = math.log(1.0 + (sell_price - entry_price) / entry_price)
        except ValueError:
            skipped_math += 1
            continue

        # Check for duplicate: a sell on this (wallet, asset_id) at the same time
        dup_check = await session.execute(
            select(sqlfunc.count())
            .select_from(WalletClosedPosition)
            .where(
                WalletClosedPosition.wallet == row.wallet,
                WalletClosedPosition.asset_id == row.asset_id,
                WalletClosedPosition.is_resolved == False,  # noqa: E712
                WalletClosedPosition.closed_at == row.ts,
            )
        )
        if dup_check.scalar_one() > 0:
            continue

        if row.wallet not in wallet_notionals:
            wallet_notionals[row.wallet] = await _get_wallet_lifetime_notional(session, row.wallet)
        lifetime_notional = wallet_notionals[row.wallet]
        conviction = cost_basis / lifetime_notional if lifetime_notional > 0 else 0.0

        session.add(WalletClosedPosition(
            wallet=row.wallet,
            asset_id=row.asset_id,
            market_id=row.market_id,
            entry_price=entry_price,
            exit_price=sell_price,
            shares_sold=shares,
            total_shares_at_time=shares,
            position_fraction=1.0,
            cost_basis=cost_basis,
            conviction_weight=conviction,
            log_roi=log_roi_val,
            realized_pnl=realized_pnl_val,
            is_resolved=False,
            closed_at=row.ts,
        ))
        written += 1

    logger.info("backfill_sell_exits", written=written, skipped_math=skipped_math)
    return written


async def run_backfill() -> dict:
    """Run the full historical backfill and trigger score recomputation."""
    started = datetime.now(tz=timezone.utc)
    print(f"=== Scoring Backfill — started {started.strftime('%Y-%m-%d %H:%M:%S UTC')} ===\n")

    factory = get_session_factory()

    # Phase 1: Resolved BUY trades
    print("[Phase 1] Backfilling from resolved BUY trades…")
    async with factory() as session:
        resolved_written = await backfill_resolved_buys(session)
        await session.commit()
    print(f"  Written: {resolved_written}")

    # Phase 2: SELL trades with exit_skill
    print("[Phase 2] Backfilling from historical SELL trades (exit_skill)…")
    async with factory() as session:
        sell_written = await backfill_sell_exits(session)
        await session.commit()
    print(f"  Written: {sell_written}")

    total_written = resolved_written + sell_written

    # Phase 3: Count rows and run scorer
    async with factory() as session:
        count_result = await session.execute(
            select(sqlfunc.count()).select_from(WalletClosedPosition)
        )
        total_rows = count_result.scalar_one()

    print(f"\n[Phase 3] Total wallet_closed_positions rows: {total_rows}")
    print("[Phase 4] Running full wallet score recomputation…")

    try:
        from app.services.wallets.scorer import run_all_wallet_scores
        async with factory() as session:
            score_result = await run_all_wallet_scores(session)
            await session.commit()
        wallets_scored = score_result.get("wallets_scored", 0)
        print(f"  Wallets scored: {wallets_scored}")
    except Exception as exc:
        logger.error("backfill_scoring_error", error=str(exc))
        print(f"  Scoring error: {exc}")
        wallets_scored = 0

    elapsed = (datetime.now(tz=timezone.utc) - started).total_seconds()
    print(f"\n=== Summary ===")
    print(f"  Resolved BUY rows written  : {resolved_written}")
    print(f"  SELL exit rows written     : {sell_written}")
    print(f"  Total closed positions     : {total_rows}")
    print(f"  Wallets scored             : {wallets_scored}")
    print(f"  Elapsed                    : {elapsed:.1f}s")

    return {
        "resolved_written": resolved_written,
        "sell_written": sell_written,
        "total_rows": total_rows,
        "wallets_scored": wallets_scored,
    }


async def main() -> None:
    setup_logging()
    await run_backfill()


if __name__ == "__main__":
    if sys.platform == "win32":
        asyncio.run(main(), loop_factory=asyncio.SelectorEventLoop)
    else:
        asyncio.run(main())
