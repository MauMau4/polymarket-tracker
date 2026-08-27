"""
Sell trade processing: cost basis tracking and closed position recording.

BUY trades update the running cost basis per (wallet, asset_id).
SELL trades snapshot the cost basis, write a WalletClosedPosition row,
then reduce the cost basis.
"""
from datetime import datetime, timezone

from sqlalchemy import select, func as sqlfunc
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models.trade import Trade
from app.db.models.wallet_closed_position import WalletClosedPosition
from app.db.models.wallet_cost_basis import WalletCostBasis
from app.logging import get_logger
from app.services.wallets.scorer import compute_log_roi

logger = get_logger(__name__)


async def close_cost_basis_if_resolved(
    session: AsyncSession,
    wallet: str,
    asset_id: str,
    market_id: str,
    ts: datetime,
) -> bool:
    """
    Called immediately after update_buy_position when a BUY trade is ingested.
    If the market is already resolved, writes a WalletClosedPosition and deletes
    the cost_basis row so it never strands.  Returns True if the position was closed.

    Duplicate-safe: skips writing if a resolved WCP row already exists for
    (wallet, asset_id, market_id).
    """
    from app.db.models.market import Market
    from app.db.models.token import Token

    if not market_id:
        return False

    mkt_result = await session.execute(
        select(Market.resolved, Market.resolution, Market.end_date, Market.updated_at, Market.resolved_at)
        .where(Market.market_id == market_id)
    )
    market_row = mkt_result.one_or_none()
    if market_row is None or not market_row.resolved or not market_row.resolution:
        return False

    pos_result = await session.execute(
        select(WalletCostBasis).where(
            WalletCostBasis.wallet == wallet,
            WalletCostBasis.asset_id == asset_id,
        )
    )
    pos = pos_result.scalar_one_or_none()
    if pos is None or pos.total_size <= 0 or pos.avg_entry_price <= 0:
        if pos and pos.total_size <= 0:
            await session.delete(pos)
        return False

    # Duplicate guard: only skip if an existing resolved WCP row has the same
    # share count (±0.001).  A wallet can have multiple resolved closure events
    # on the same asset with different share amounts.
    dup_result = await session.execute(
        select(sqlfunc.count())
        .select_from(WalletClosedPosition)
        .where(
            WalletClosedPosition.wallet == wallet,
            WalletClosedPosition.asset_id == asset_id,
            WalletClosedPosition.market_id == market_id,
            WalletClosedPosition.is_resolved == True,  # noqa: E712
            sqlfunc.abs(WalletClosedPosition.shares_sold - pos.total_size) < 0.001,
        )
    )
    if dup_result.scalar_one() > 0:
        await session.delete(pos)
        return False

    tok_result = await session.execute(
        select(Token.outcome).where(Token.asset_id == asset_id)
    )
    token_outcome = tok_result.scalar_one_or_none()
    if token_outcome is None:
        logger.warning(
            "close_resolved_missing_token_outcome",
            wallet=wallet,
            asset_id=asset_id,
            market_id=market_id,
        )
        return False

    resolution_str = market_row.resolution.upper()
    outcome_str = (token_outcome or "").upper()
    exit_price = 1.0 if outcome_str == resolution_str else 0.0

    log_roi_val = compute_log_roi(exit_price, pos.avg_entry_price) or 0.0
    cost_basis_val = pos.avg_entry_price * pos.total_size
    realized_pnl_val = (exit_price - pos.avg_entry_price) * pos.total_size

    lifetime_row = await session.execute(
        select(sqlfunc.sum(Trade.notional_usd))
        .where(Trade.wallet == wallet, Trade.notional_usd.is_not(None), Trade.side == "BUY")
    )
    lifetime_notional = float(lifetime_row.scalar_one_or_none() or 0.0)
    conviction = cost_basis_val / lifetime_notional if lifetime_notional > 0 else 0.0

    # closed_at = true settlement time, never end_date (see docs/m1-schema-audit.md).
    # Falls back to detection time (now), never end_date, so this is never
    # earlier than we could actually have known the outcome.
    closed_at = market_row.resolved_at or datetime.now(tz=timezone.utc)
    closed_at_source = "gamma_settlement" if market_row.resolved_at is not None else "detection_time_proxy"

    session.add(WalletClosedPosition(
        wallet=wallet,
        asset_id=asset_id,
        market_id=market_id,
        entry_price=pos.avg_entry_price,
        exit_price=exit_price,
        shares_sold=pos.total_size,
        total_shares_at_time=pos.total_size,
        position_fraction=1.0,
        cost_basis=cost_basis_val,
        conviction_weight=conviction,
        log_roi=log_roi_val,
        realized_pnl=realized_pnl_val,
        is_resolved=True,
        closed_at_source=closed_at_source,
        closed_at=closed_at,
    ))
    shares_closed = pos.total_size
    await session.delete(pos)

    # Also close the wallet_positions row — the market is resolved, so nothing
    # else will ever close it.
    from app.services.wallets.positions import close_position_on_resolution
    await close_position_on_resolution(
        session,
        wallet=wallet,
        asset_id=asset_id,
        realized_pnl=realized_pnl_val,
        remaining_shares=shares_closed,
        closed_at=closed_at,
    )

    logger.info(
        "cost_basis_closed_at_ingestion",
        wallet=wallet,
        asset_id=asset_id,
        market_id=market_id,
        exit_price=exit_price,
        shares=shares_closed,
    )
    return True


async def update_buy_position(
    session: AsyncSession,
    wallet: str,
    asset_id: str,
    market_id: str,
    price: float,
    size: float,
) -> None:
    """Update or create the running cost basis when a BUY trade is ingested."""
    result = await session.execute(
        select(WalletCostBasis).where(
            WalletCostBasis.wallet == wallet,
            WalletCostBasis.asset_id == asset_id,
        )
    )
    pos = result.scalar_one_or_none()
    now = datetime.now(tz=timezone.utc)

    if pos is None:
        session.add(WalletCostBasis(
            wallet=wallet,
            asset_id=asset_id,
            market_id=market_id,
            avg_entry_price=price,
            total_size=size,
            updated_at=now,
        ))
    else:
        new_total = pos.total_size + size
        if new_total > 0:
            pos.avg_entry_price = (
                pos.avg_entry_price * pos.total_size + price * size
            ) / new_total
        pos.total_size = new_total
        pos.updated_at = now


async def write_sell_closed_position(
    session: AsyncSession,
    wallet: str,
    asset_id: str,
    market_id: str,
    price: float,
    size: float,
    ts: datetime,
) -> float:
    """
    Process a SELL trade: write a WalletClosedPosition row, then reduce cost basis.

    Skips writing if no position exists, entry_price is zero, or log_roi is
    undefined. The cost basis reduction still occurs when a position is found.
    Returns realized_pnl (0.0 if skipped).
    """
    result = await session.execute(
        select(WalletCostBasis).where(
            WalletCostBasis.wallet == wallet,
            WalletCostBasis.asset_id == asset_id,
        )
    )
    pos = result.scalar_one_or_none()
    if pos is None or pos.total_size <= 0:
        # No buy history — no verified cost basis, so no WCP row. Writing a
        # placeholder (entry = exit, log_roi = 0) polluted scoring: those rows
        # counted as losses in win_rate and diluted consistency. The sell stays
        # visible in the UI via the wallet_positions placeholder created by
        # update_position_on_sell.
        logger.debug(
            "sell_no_prior_buy_skipping_wcp",
            wallet=wallet, asset_id=asset_id, market_id=market_id,
        )
        return 0.0

    entry_price = pos.avg_entry_price
    total_shares_before = pos.total_size

    if entry_price <= 0:
        logger.debug("sell_zero_entry_price", wallet=wallet, asset_id=asset_id)
        # Reduce position even though we skip the closed-position row
        _reduce_position(pos, size)
        return 0.0

    log_roi_val = compute_log_roi(price, entry_price) or 0.0

    # Cap at the tracked position size: shares beyond it have no observed cost
    # basis (missed BUYs), so they contribute no verified PnL.
    effective_size = min(size, total_shares_before)
    position_fraction = effective_size / total_shares_before if total_shares_before > 0 else 0.0
    cost_basis = entry_price * effective_size
    realized_pnl_val = (price - entry_price) * effective_size

    # Wallet lifetime BUY-only notional for conviction_weight
    lifetime_row = await session.execute(
        select(sqlfunc.sum(Trade.notional_usd))
        .where(Trade.wallet == wallet, Trade.notional_usd.is_not(None), Trade.side == "BUY")
    )
    lifetime_notional = float(lifetime_row.scalar_one_or_none() or 0.0)
    conviction = cost_basis / lifetime_notional if lifetime_notional > 0 else 0.0

    session.add(WalletClosedPosition(
        wallet=wallet,
        asset_id=asset_id,
        market_id=market_id,
        entry_price=entry_price,
        exit_price=price,
        shares_sold=effective_size,
        total_shares_at_time=total_shares_before,
        position_fraction=position_fraction,
        cost_basis=cost_basis,
        conviction_weight=conviction,
        log_roi=log_roi_val,
        realized_pnl=realized_pnl_val,
        is_resolved=False,
        closed_at_source="observed_exit",
        closed_at=ts,
    ))

    logger.debug(
        "sell_closed_position_written",
        wallet=wallet,
        asset_id=asset_id,
        entry_price=entry_price,
        exit_price=price,
        log_roi=log_roi_val,
    )

    _reduce_position(pos, size)
    return realized_pnl_val


def _reduce_position(pos: WalletCostBasis, size: float) -> None:
    new_size = max(0.0, pos.total_size - size)
    if new_size <= 0:
        # Will be deleted by the session on flush — mark size to 0 so
        # subsequent checks see an empty position.
        pos.total_size = 0.0
    else:
        pos.total_size = new_size
        pos.updated_at = datetime.now(tz=timezone.utc)
