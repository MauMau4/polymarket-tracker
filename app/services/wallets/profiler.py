"""Wallet profile creation and maintenance."""
from datetime import datetime

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models.market import Market
from app.db.models.trade import Trade
from app.db.models.wallet import Wallet
from app.logging import get_logger

logger = get_logger(__name__)


async def ensure_wallet(
    session: AsyncSession,
    wallet_address: str,
    ts: datetime,
) -> Wallet:
    """Upsert a wallet row. Creates it if not seen before. Caller must commit."""
    result = await session.execute(select(Wallet).where(Wallet.wallet == wallet_address))
    wallet = result.scalar_one_or_none()

    if wallet is None:
        wallet = Wallet(
            wallet=wallet_address,
            first_seen=ts,
            last_seen=ts,
            markets_traded_count=0,
            resolved_markets_count=0,
            wallet_score=0.0,
            score_confidence="low",
        )
        session.add(wallet)
        logger.info("wallet_created", wallet=wallet_address)
    else:
        if wallet.last_seen is None or ts > wallet.last_seen:
            wallet.last_seen = ts

    return wallet


async def update_wallet_activity(session: AsyncSession, wallet_address: str) -> None:
    """Recompute activity stats for a wallet from its trade history. Caller must commit."""
    markets_result = await session.execute(
        select(func.count(func.distinct(Trade.market_id))).where(
            Trade.wallet == wallet_address
        )
    )
    markets_count: int = markets_result.scalar() or 0

    resolved_result = await session.execute(
        select(func.count(func.distinct(Trade.market_id)))
        .join(Market, Market.market_id == Trade.market_id)
        .where(Trade.wallet == wallet_address)
        .where(Market.resolved == True)  # noqa: E712
    )
    resolved_count: int = resolved_result.scalar() or 0

    notional_rows = (
        await session.execute(
            select(Trade.market_id, func.sum(Trade.notional_usd).label("total"))
            .where(Trade.wallet == wallet_address)
            .where(Trade.notional_usd.is_not(None))
            .group_by(Trade.market_id)
            .order_by(func.sum(Trade.notional_usd).desc())
        )
    ).all()

    concentration_score: float | None = None
    if notional_rows:
        total_notional = sum(float(r.total) for r in notional_rows)
        if total_notional > 0:
            top_market = float(notional_rows[0].total)
            concentration_score = (top_market / total_notional) * 100.0

    # Each resolved market = 10 points, capped at 100
    consistency_score: float | None = (
        min(100.0, resolved_count * 10.0) if markets_count > 0 else None
    )

    wallet_result = await session.execute(select(Wallet).where(Wallet.wallet == wallet_address))
    wallet = wallet_result.scalar_one_or_none()
    if wallet is None:
        return

    wallet.markets_traded_count = markets_count
    wallet.resolved_markets_count = resolved_count
    wallet.concentration_score = concentration_score
    wallet.consistency_score = consistency_score

    logger.debug(
        "wallet_activity_updated",
        wallet=wallet_address,
        markets_traded=markets_count,
        resolved=resolved_count,
        concentration=concentration_score,
        consistency=consistency_score,
    )
