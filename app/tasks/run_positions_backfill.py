"""
Backfill wallet_positions from existing trades and wallet_closed_positions data.

Replays all trades per (wallet, asset_id) in chronological order to build
the final position state. Resolves position status from existing WCP rows.

Safe to run multiple times — truncates wallet_positions and rebuilds from scratch.

Usage:
    python -m app.tasks.run_positions_backfill
"""
import asyncio
import sys
from datetime import datetime, timezone

from sqlalchemy import delete, select, text

from app.db.models.trade import Trade
from app.db.models.wallet_closed_position import WalletClosedPosition
from app.db.models.wallet_position import WalletPosition
from app.db.session import get_session_factory
from app.logging import setup_logging, get_logger

logger = get_logger(__name__)


async def run_positions_backfill() -> dict:
    """
    Rebuild wallet_positions from all existing trade history.
    Returns summary dict.
    """
    now = datetime.now(tz=timezone.utc)
    factory = get_session_factory()

    # Step 1: find all distinct (wallet, asset_id) pairs with BUY trades
    async with factory() as session:
        pairs_result = await session.execute(
            select(Trade.wallet, Trade.asset_id)
            .where(
                Trade.wallet.is_not(None),
                Trade.side == "BUY",
                Trade.price.is_not(None),
                Trade.size.is_not(None),
            )
            .distinct()
        )
        pairs = pairs_result.all()

    if not pairs:
        logger.info("positions_backfill_no_pairs")
        return {"pairs": 0, "written": 0}

    logger.info("positions_backfill_pairs_found", count=len(pairs))

    # Step 2: truncate existing rows
    async with factory() as session:
        await session.execute(delete(WalletPosition))
        await session.commit()
    logger.info("positions_backfill_truncated")

    written = 0
    errors = 0

    for wallet, asset_id in pairs:
        try:
            async with factory() as session:
                # Load all trades for this (wallet, asset_id) ordered by ts ASC
                trades_result = await session.execute(
                    select(Trade)
                    .where(
                        Trade.wallet == wallet,
                        Trade.asset_id == asset_id,
                        Trade.price.is_not(None),
                        Trade.size.is_not(None),
                    )
                    .order_by(Trade.ts.asc())
                )
                trades = trades_result.scalars().all()

                if not trades:
                    continue

                # Load SELL WCP rows for this (wallet, asset_id) for realized_pnl
                sell_wcp_result = await session.execute(
                    select(WalletClosedPosition)
                    .where(
                        WalletClosedPosition.wallet == wallet,
                        WalletClosedPosition.asset_id == asset_id,
                        WalletClosedPosition.is_resolved == False,  # noqa: E712
                    )
                    .order_by(WalletClosedPosition.closed_at.asc())
                )
                sell_wcps = sell_wcp_result.scalars().all()
                # Index SELL WCP by closed_at for matching
                sell_wcp_queue = list(sell_wcps)

                # Load resolved WCP rows
                resolved_wcp_result = await session.execute(
                    select(WalletClosedPosition)
                    .where(
                        WalletClosedPosition.wallet == wallet,
                        WalletClosedPosition.asset_id == asset_id,
                        WalletClosedPosition.is_resolved == True,  # noqa: E712
                    )
                    .order_by(WalletClosedPosition.closed_at.asc())
                )
                resolved_wcps = resolved_wcp_result.scalars().all()

                # Replay trades
                total_bought = 0.0
                total_sold = 0.0
                remaining = 0.0
                avg_entry = 0.0
                total_cost_basis = 0.0
                realized_pnl = 0.0
                opened_at = None
                market_id = trades[0].market_id or ""
                selection = trades[0].outcome

                # Match SELL trades to WCP rows (closest by timestamp)
                used_wcp_indices: set[int] = set()

                def _find_sell_wcp(ts) -> WalletClosedPosition | None:
                    best_idx = None
                    best_delta = None
                    for i, wcp in enumerate(sell_wcp_queue):
                        if i in used_wcp_indices:
                            continue
                        try:
                            delta = abs((wcp.closed_at - ts).total_seconds())
                        except Exception:
                            continue
                        if best_delta is None or delta < best_delta:
                            best_delta = delta
                            best_idx = i
                    if best_idx is not None and best_delta is not None and best_delta <= 30:
                        used_wcp_indices.add(best_idx)
                        return sell_wcp_queue[best_idx]
                    return None

                for trade in trades:
                    price = float(trade.price)
                    size = float(trade.size)
                    side = (trade.side or "").upper()

                    if side == "BUY":
                        if opened_at is None:
                            opened_at = trade.ts
                        # Update avg entry using remaining shares
                        new_total = remaining + size
                        if new_total > 0:
                            avg_entry = (remaining * avg_entry + size * price) / new_total
                        total_bought += size
                        remaining = new_total
                        total_cost_basis = avg_entry * total_bought

                    elif side == "SELL":
                        wcp = _find_sell_wcp(trade.ts)
                        sell_pnl = wcp.realized_pnl if wcp else (price - avg_entry) * size
                        total_sold += size
                        remaining = max(0.0, remaining - size)
                        realized_pnl += sell_pnl

                if opened_at is None:
                    continue

                # Determine status from resolved WCP rows
                status = "Open"
                closed_at = None

                if resolved_wcps:
                    # Sum realized_pnl from all resolved WCP rows (may be multiple)
                    for rwcp in resolved_wcps:
                        realized_pnl += rwcp.realized_pnl
                        total_sold += rwcp.shares_sold
                    remaining = max(0.0, total_bought - total_sold)
                    if remaining <= 0:
                        status = "Closed"
                        closed_at = resolved_wcps[-1].closed_at

                elif remaining <= 0 and total_sold > 0:
                    status = "Closed"
                    # Find closed_at from last SELL WCP
                    matched = [sell_wcp_queue[i] for i in used_wcp_indices]
                    if matched:
                        closed_at = max(w.closed_at for w in matched)

                session.add(WalletPosition(
                    wallet=wallet,
                    asset_id=asset_id,
                    market_id=market_id,
                    selection=selection,
                    total_shares_bought=total_bought,
                    total_shares_sold=total_sold,
                    remaining_shares=remaining,
                    avg_entry_price=avg_entry,
                    total_cost_basis=total_cost_basis,
                    realized_pnl=realized_pnl,
                    status=status,
                    opened_at=opened_at,
                    closed_at=closed_at,
                ))
                await session.commit()
                written += 1

        except Exception as exc:
            logger.error(
                "positions_backfill_error",
                wallet=wallet,
                asset_id=asset_id,
                error=str(exc),
                exc_info=True,
            )
            errors += 1

    logger.info("positions_backfill_complete", written=written, errors=errors)
    return {"pairs": len(pairs), "written": written, "errors": errors}


async def main() -> None:
    setup_logging()
    started = datetime.now(tz=timezone.utc)
    print(f"=== Positions Backfill — started {started.strftime('%Y-%m-%d %H:%M:%S UTC')} ===\n")

    result = await run_positions_backfill()

    print(f"\n=== Summary ===")
    print(f"  (wallet, asset_id) pairs  : {result['pairs']}")
    print(f"  wallet_positions written  : {result['written']}")
    print(f"  Errors                    : {result.get('errors', 0)}")

    elapsed = (datetime.now(tz=timezone.utc) - started).total_seconds()
    print(f"  Elapsed                   : {elapsed:.1f}s")


if __name__ == "__main__":
    if sys.platform == "win32":
        asyncio.run(main(), loop_factory=asyncio.SelectorEventLoop)
    else:
        asyncio.run(main())
