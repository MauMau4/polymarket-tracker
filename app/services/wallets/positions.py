"""
wallet_positions table maintenance.

Called from normalizer.py (BUY/SELL) and resolution.py (market resolution).
Maintains a live running record of each wallet's position in every asset.
"""
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models.wallet_position import WalletPosition
from app.logging import get_logger

logger = get_logger(__name__)


async def update_position_on_buy(
    session: AsyncSession,
    wallet: str,
    asset_id: str,
    market_id: str,
    price: float,
    size: float,
    ts: datetime,
    selection: str | None = None,
) -> None:
    """Upsert position on a BUY trade."""
    result = await session.execute(
        select(WalletPosition).where(
            WalletPosition.wallet == wallet,
            WalletPosition.asset_id == asset_id,
        )
    )
    pos = result.scalar_one_or_none()

    if pos is None:
        session.add(WalletPosition(
            wallet=wallet,
            asset_id=asset_id,
            market_id=market_id,
            selection=selection,
            total_shares_bought=size,
            total_shares_sold=0.0,
            remaining_shares=size,
            avg_entry_price=price,
            total_cost_basis=price * size,
            realized_pnl=0.0,
            status="Open",
            opened_at=ts,
        ))
    else:
        # Avg entry uses remaining (post-sell) shares, not total
        remaining = max(0.0, pos.remaining_shares)
        new_total = remaining + size
        if new_total > 0:
            pos.avg_entry_price = (remaining * pos.avg_entry_price + size * price) / new_total
        pos.total_shares_bought += size
        pos.remaining_shares = new_total
        pos.total_cost_basis = pos.avg_entry_price * pos.total_shares_bought
        pos.status = "Open"


async def update_position_on_sell(
    session: AsyncSession,
    wallet: str,
    asset_id: str,
    shares_sold: float,
    realized_pnl: float,
    ts: datetime,
    market_id: str = "",
    price: float = 0.0,
) -> None:
    """Reduce position on a SELL trade.

    When no prior wallet_positions row exists and price/market_id are provided,
    creates a placeholder Closed row (entry = sell_price, pnl = 0) so the sell
    is visible in the UI with a 0% ROI signal for missing buy data.
    """
    result = await session.execute(
        select(WalletPosition).where(
            WalletPosition.wallet == wallet,
            WalletPosition.asset_id == asset_id,
        )
    )
    pos = result.scalar_one_or_none()
    if pos is None:
        if price > 0 and market_id:
            session.add(WalletPosition(
                wallet=wallet,
                asset_id=asset_id,
                market_id=market_id,
                total_shares_bought=shares_sold,
                total_shares_sold=shares_sold,
                remaining_shares=0.0,
                avg_entry_price=price,
                total_cost_basis=price * shares_sold,
                realized_pnl=0.0,
                status="Closed",
                estimated_entry=True,
                opened_at=ts,
                closed_at=ts,
            ))
            logger.debug(
                "sell_no_prior_position_placeholder_created",
                wallet=wallet, asset_id=asset_id, market_id=market_id,
            )
        else:
            logger.debug("update_position_on_sell_no_position", wallet=wallet, asset_id=asset_id)
        return

    pos.total_shares_sold += shares_sold
    pos.remaining_shares = max(0.0, pos.remaining_shares - shares_sold)

    # Only accumulate realized_pnl when write_sell_closed_position actually wrote a
    # WCP row (signalled by a non-zero return value). A zero return means no cost basis
    # existed, so there is no verified PnL to record.
    if realized_pnl:
        pos.realized_pnl += realized_pnl

    # Guard: sells exceed buys when BUY trades were missed (app offline / pre-tracking).
    # Inflate total_bought to match so cost_basis and ROI remain meaningful.
    if pos.total_shares_sold > pos.total_shares_bought:
        pos.total_shares_bought = pos.total_shares_sold
        pos.total_cost_basis = pos.total_shares_bought * pos.avg_entry_price

    if pos.remaining_shares < 0.001:
        pos.remaining_shares = 0.0
        pos.status = "Closed"
        pos.closed_at = ts


async def close_position_on_resolution(
    session: AsyncSession,
    wallet: str,
    asset_id: str,
    realized_pnl: float,
    remaining_shares: float,
    closed_at: datetime,
) -> None:
    """Close position on market resolution (settles the remaining open shares)."""
    result = await session.execute(
        select(WalletPosition).where(
            WalletPosition.wallet == wallet,
            WalletPosition.asset_id == asset_id,
        )
    )
    pos = result.scalar_one_or_none()
    if pos is None:
        logger.debug(
            "close_position_on_resolution_no_position",
            wallet=wallet,
            asset_id=asset_id,
        )
        return

    pos.total_shares_sold += remaining_shares
    pos.remaining_shares = 0.0
    pos.realized_pnl += realized_pnl

    # Guard: sells exceed buys when BUY trades were missed (app offline / pre-tracking).
    if pos.total_shares_sold > pos.total_shares_bought:
        pos.total_shares_bought = pos.total_shares_sold
        pos.total_cost_basis = pos.total_shares_bought * pos.avg_entry_price

    pos.status = "Closed"
    pos.closed_at = closed_at
