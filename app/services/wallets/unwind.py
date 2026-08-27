"""
Position unwind detection — Phase 5.

Flags a trade as a potential unwind when:
  • The same wallet trades the OPPOSITE side of an outcome they previously held
  • The new trade size is >= 50% of the wallet's prior cumulative buy size for that outcome
  • The current price has moved >= 0.05 from the wallet's average entry price

Fires a POSITION_UNWIND alert with: wallet, market, original entry, current price,
implied P&L direction. Also tags the trade's trade_type as 'unwind'.
"""
import uuid
from datetime import datetime, timezone

from sqlalchemy import select, func

from app.config import get_settings
from app.db.models import Alert
from app.db.models.trade import Trade
from app.db.session import get_session_factory
from app.logging import get_logger
from app.schemas.trade import NormalizedTrade
from app.services.alerts import telegram

logger = get_logger(__name__)

_UNWIND_SIZE_THRESHOLD = 0.50   # new trade must be >= 50% of prior position size
_UNWIND_PRICE_MOVE = 0.05       # price must have moved >= 0.05 from average entry
_DEDUPE_PREFIX = "unwind"


async def _get_redis():
    import redis.asyncio as redis_async
    settings = get_settings()
    return redis_async.from_url(settings.redis_url)


async def _is_deduped(dedupe_key: str) -> bool:
    try:
        r = await _get_redis()
        return bool(await r.exists(f"{_DEDUPE_PREFIX}:{dedupe_key}"))
    except Exception as exc:
        logger.warning("unwind_dedupe_check_error", error=str(exc))
        return False


async def _mark_deduped(dedupe_key: str, cooldown: int) -> None:
    try:
        r = await _get_redis()
        await r.setex(f"{_DEDUPE_PREFIX}:{dedupe_key}", cooldown, "1")
    except Exception as exc:
        logger.warning("unwind_dedupe_mark_error", error=str(exc))


def _opposite_side(side: str | None) -> str | None:
    if side is None:
        return None
    s = side.upper()
    if s == "BUY":
        return "SELL"
    if s == "SELL":
        return "BUY"
    return None


async def check_unwind(trade: NormalizedTrade) -> None:
    """
    Check whether this trade is an unwind of a prior position.
    Called after a trade is persisted. No-op for unattributed trades.
    """
    if not trade.wallet or not trade.outcome or trade.price is None or trade.size is None:
        return
    if not trade.side:
        return

    settings = get_settings()
    wallet = trade.wallet
    market_id = trade.market_id
    outcome = (trade.outcome or "").upper()
    current_price = float(trade.price)
    current_size = float(trade.size)
    current_side = (trade.side or "").upper()

    # We only detect unwinds for SELL trades (or buying the opposite outcome)
    # Simplification: flag when side = SELL and they previously bought the same outcome
    if current_side != "SELL":
        return

    factory = get_session_factory()
    async with factory() as session:
        # Find previous BUY trades by this wallet on the same market+outcome
        prior = await session.execute(
            select(
                func.sum(Trade.size).label("total_size"),
                func.avg(Trade.price).label("avg_price"),
                func.count().label("trade_count"),
            )
            .where(
                Trade.wallet == wallet,
                Trade.market_id == market_id,
                Trade.outcome == outcome,
                Trade.side == "BUY",
                Trade.trade_type.is_(None),
            )
        )
        row = prior.one()

    if row.trade_count == 0 or row.total_size is None:
        return

    prior_total_size = float(row.total_size)
    avg_entry = float(row.avg_price) if row.avg_price else None

    if prior_total_size <= 0:
        return

    # Size check: new sell >= 50% of prior buys
    if current_size < prior_total_size * _UNWIND_SIZE_THRESHOLD:
        return

    # Price move check
    if avg_entry is None:
        return
    price_move = abs(current_price - avg_entry)
    if price_move < _UNWIND_PRICE_MOVE:
        return

    # Determine implied P&L direction
    # Selling higher than entry = profit; selling lower = loss
    pnl_direction = "profit" if current_price > avg_entry else "loss"

    dedupe_key = f"{wallet}:{market_id}:{outcome}:{int(trade.ts.timestamp()) // 3600}"

    if await _is_deduped(dedupe_key):
        logger.debug("unwind_deduped", wallet=wallet, market_id=market_id)
        return

    logger.info(
        "position_unwind_detected",
        wallet=wallet,
        market_id=market_id,
        outcome=outcome,
        avg_entry=avg_entry,
        current_price=current_price,
        price_move=price_move,
        pnl_direction=pnl_direction,
    )

    message = (
        f"[POSITION_UNWIND]\n\n"
        f"Wallet: {wallet[:10]}…{wallet[-6:]}\n"
        f"Market: {market_id}\n"
        f"Outcome: {outcome}\n"
        f"Entry: {avg_entry:.3f} → Exit: {current_price:.3f} "
        f"(move: {price_move:+.3f}, implied {pnl_direction})\n"
        f"Unwind size: {current_size:.1f} / prior {prior_total_size:.1f} tokens"
    )

    factory = get_session_factory()
    async with factory() as session:
        # Tag the trade as unwind
        trade_result = await session.execute(
            select(Trade)
            .where(
                Trade.wallet == wallet,
                Trade.market_id == market_id,
                Trade.ts == trade.ts,
                Trade.side == current_side,
            )
            .limit(1)
        )
        trade_obj = trade_result.scalar_one_or_none()
        if trade_obj:
            trade_obj.trade_type = "unwind"

        # Persist alert
        alert = Alert(
            id=str(uuid.uuid4()),
            alert_type="POSITION_UNWIND",
            severity="medium",
            wallet=wallet,
            market_id=market_id,
            asset_id=trade.asset_id,
            title=f"POSITION_UNWIND — {wallet[:10]}…",
            message=message,
            payload={
                "wallet": wallet,
                "market_id": market_id,
                "outcome": outcome,
                "avg_entry": avg_entry,
                "current_price": current_price,
                "price_move": price_move,
                "pnl_direction": pnl_direction,
                "prior_size": prior_total_size,
                "unwind_size": current_size,
            },
            dedupe_key=dedupe_key,
            sent=False,
            sent_ts=None,
        )
        session.add(alert)
        await session.commit()

    sent = await telegram.send_message(message)
    if sent:
        factory = get_session_factory()
        async with factory() as session:
            result = await session.execute(select(Alert).where(Alert.dedupe_key == dedupe_key))
            saved = result.scalar_one_or_none()
            if saved:
                saved.sent = True
                saved.sent_ts = datetime.now(tz=timezone.utc)
                await session.commit()

    await _mark_deduped(dedupe_key, settings.default_alert_cooldown_seconds)
