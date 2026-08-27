"""
One-time cleanup: fix wallet_positions rows where total_shares_sold > total_shares_bought.

Occurs when BUY trades were missed (app offline or trades arrived before tracking began).
The SELL-side data (from wallet_closed_positions) is correct; only total_shares_bought
and total_cost_basis need adjustment.

Fix:
  total_shares_bought = total_shares_sold          (inflate to match)
  total_cost_basis    = total_shares_bought × avg_entry_price
  realized_pnl        = unchanged (sourced from WCP, already correct)

Safe to run multiple times.

Usage:
    python -m app.tasks.run_positions_cost_basis_fix
"""
import asyncio
import sys
from datetime import datetime, timezone

from sqlalchemy import select

from app.db.models.wallet_position import WalletPosition
from app.db.session import get_session_factory
from app.logging import setup_logging, get_logger

logger = get_logger(__name__)


async def run_positions_cost_basis_fix() -> dict:
    factory = get_session_factory()

    async with factory() as session:
        result = await session.execute(
            select(WalletPosition).where(
                WalletPosition.total_shares_sold > WalletPosition.total_shares_bought
            )
        )
        bad_positions = result.scalars().all()

    logger.info("positions_cost_basis_fix_found", count=len(bad_positions))

    fixed = 0
    errors = 0

    for pos in bad_positions:
        try:
            async with factory() as session:
                p = (await session.execute(
                    select(WalletPosition).where(WalletPosition.id == pos.id)
                )).scalar_one_or_none()
                if p is None:
                    continue

                old_bought = p.total_shares_bought
                p.total_shares_bought = p.total_shares_sold
                p.total_cost_basis = p.total_shares_bought * p.avg_entry_price

                logger.info(
                    "positions_cost_basis_fix_applied",
                    wallet=p.wallet,
                    asset_id=p.asset_id,
                    old_shares_bought=old_bought,
                    new_shares_bought=p.total_shares_bought,
                    new_cost_basis=p.total_cost_basis,
                    realized_pnl=p.realized_pnl,
                )
                await session.commit()
                fixed += 1

        except Exception as exc:
            logger.error(
                "positions_cost_basis_fix_error",
                wallet=pos.wallet,
                asset_id=pos.asset_id,
                error=str(exc),
            )
            errors += 1

    logger.info("positions_cost_basis_fix_complete", fixed=fixed, errors=errors)
    return {"found": len(bad_positions), "fixed": fixed, "errors": errors}


async def main() -> None:
    setup_logging()
    started = datetime.now(tz=timezone.utc)
    print(f"=== Positions Cost Basis Fix — started {started.strftime('%Y-%m-%d %H:%M:%S UTC')} ===\n")

    result = await run_positions_cost_basis_fix()

    print(f"\n=== Summary ===")
    print(f"  Rows found   : {result['found']}")
    print(f"  Rows fixed   : {result['fixed']}")
    print(f"  Errors       : {result['errors']}")

    elapsed = (datetime.now(tz=timezone.utc) - started).total_seconds()
    print(f"  Elapsed      : {elapsed:.1f}s")


if __name__ == "__main__":
    if sys.platform == "win32":
        asyncio.run(main(), loop_factory=asyncio.SelectorEventLoop)
    else:
        asyncio.run(main())
