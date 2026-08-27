"""
One-time cleanup: fix wallet_positions rows stuck as status="Open" when they should be "Closed".

Two cases:
  1. remaining_shares <= 0.001 but status="Open"
     → set status="Closed", closed_at from latest WCP for (wallet, asset_id)
  2. status="Open" but associated market is resolved=True
     → accumulate realized_pnl from is_resolved=True WCP rows,
       set remaining_shares=0, status="Closed", closed_at from WCP

Safe to run multiple times.

Usage:
    python -m app.tasks.run_positions_status_fix
"""
import asyncio
import sys
from datetime import datetime, timezone

from sqlalchemy import select, func as sqlfunc

from app.db.models.market import Market
from app.db.models.token import Token
from app.db.models.wallet_closed_position import WalletClosedPosition
from app.db.models.wallet_position import WalletPosition
from app.db.session import get_session_factory
from app.logging import setup_logging, get_logger

logger = get_logger(__name__)


async def run_positions_status_fix() -> dict:
    now = datetime.now(tz=timezone.utc)
    factory = get_session_factory()

    fixed_zero_shares = 0
    fixed_resolved_market = 0
    errors = 0

    # ── Case 1: remaining_shares ≤ 0.001 but status = "Open" ─────────────────
    async with factory() as session:
        zero_result = await session.execute(
            select(WalletPosition)
            .where(
                WalletPosition.status == "Open",
                WalletPosition.remaining_shares <= 0.001,
            )
        )
        zero_positions = zero_result.scalars().all()

    logger.info("positions_status_fix_zero_shares_found", count=len(zero_positions))

    for pos in zero_positions:
        try:
            async with factory() as session:
                p = (await session.execute(
                    select(WalletPosition).where(WalletPosition.id == pos.id)
                )).scalar_one_or_none()
                if p is None:
                    continue

                # Find latest closed_at from any WCP for this (wallet, asset_id)
                wcp_result = await session.execute(
                    select(sqlfunc.max(WalletClosedPosition.closed_at))
                    .where(
                        WalletClosedPosition.wallet == p.wallet,
                        WalletClosedPosition.asset_id == p.asset_id,
                    )
                )
                latest_closed_at = wcp_result.scalar_one_or_none() or now

                p.status = "Closed"
                p.remaining_shares = 0.0
                p.closed_at = p.closed_at or latest_closed_at
                await session.commit()
                fixed_zero_shares += 1

        except Exception as exc:
            logger.error(
                "positions_status_fix_zero_error",
                wallet=pos.wallet,
                asset_id=pos.asset_id,
                error=str(exc),
            )
            errors += 1

    # ── Case 2: status = "Open" but market is already resolved ───────────────
    async with factory() as session:
        open_on_resolved_result = await session.execute(
            select(WalletPosition)
            .join(Market, Market.market_id == WalletPosition.market_id)
            .where(
                WalletPosition.status == "Open",
                Market.resolved == True,  # noqa: E712
                Market.resolution.is_not(None),
            )
        )
        open_on_resolved = open_on_resolved_result.scalars().all()

    logger.info("positions_status_fix_resolved_market_found", count=len(open_on_resolved))

    for pos in open_on_resolved:
        try:
            async with factory() as session:
                p = (await session.execute(
                    select(WalletPosition).where(WalletPosition.id == pos.id)
                )).scalar_one_or_none()
                if p is None or p.status == "Closed":
                    continue

                # Sum realized_pnl from all WCP rows for this (wallet, asset_id)
                wcp_pnl_result = await session.execute(
                    select(
                        sqlfunc.sum(WalletClosedPosition.realized_pnl),
                        sqlfunc.max(WalletClosedPosition.closed_at),
                        sqlfunc.sum(WalletClosedPosition.shares_sold),
                    )
                    .where(
                        WalletClosedPosition.wallet == p.wallet,
                        WalletClosedPosition.asset_id == p.asset_id,
                    )
                )
                row = wcp_pnl_result.one_or_none()
                total_wcp_pnl = float(row[0] or 0.0) if row else 0.0
                latest_closed_at = row[1] if row else now
                total_wcp_shares = float(row[2] or 0.0) if row else 0.0

                p.realized_pnl = total_wcp_pnl
                p.total_shares_sold = total_wcp_shares
                p.remaining_shares = max(0.0, p.total_shares_bought - total_wcp_shares)
                p.status = "Closed"
                p.closed_at = p.closed_at or latest_closed_at or now
                await session.commit()
                fixed_resolved_market += 1

        except Exception as exc:
            logger.error(
                "positions_status_fix_resolved_error",
                wallet=pos.wallet,
                asset_id=pos.asset_id,
                error=str(exc),
            )
            errors += 1

    logger.info(
        "positions_status_fix_complete",
        fixed_zero_shares=fixed_zero_shares,
        fixed_resolved_market=fixed_resolved_market,
        errors=errors,
    )
    return {
        "fixed_zero_shares": fixed_zero_shares,
        "fixed_resolved_market": fixed_resolved_market,
        "errors": errors,
    }


async def main() -> None:
    setup_logging()
    started = datetime.now(tz=timezone.utc)
    print(f"=== Positions Status Fix — started {started.strftime('%Y-%m-%d %H:%M:%S UTC')} ===\n")

    result = await run_positions_status_fix()

    print(f"\n=== Summary ===")
    print(f"  Fixed (zero shares)    : {result['fixed_zero_shares']}")
    print(f"  Fixed (resolved market): {result['fixed_resolved_market']}")
    print(f"  Errors                 : {result['errors']}")

    elapsed = (datetime.now(tz=timezone.utc) - started).total_seconds()
    print(f"  Elapsed                : {elapsed:.1f}s")


if __name__ == "__main__":
    if sys.platform == "win32":
        asyncio.run(main(), loop_factory=asyncio.SelectorEventLoop)
    else:
        asyncio.run(main())
