"""
Sybil cluster detection — Phase 5.

Detects coordinated wallets entering the same side of a market in tight formation:
  • 3+ distinct attributed wallets
  • Same market_id + outcome
  • Price within ±0.02 of each other
  • Size within 20% of each other (max/min ≤ 1.2)
  • All within a 30-minute rolling window

When a cluster fires:
  • Alert type: SYBIL_CLUSTER, severity: high
  • All wallets tagged suspected_sybil = True
  • ClusterEvent record saved
  • Redis-deduped so the same cluster doesn't repeat within the cooldown window

If the cluster also meets farming criteria (price > 0.90), the alert includes a
[FARMING] label — a coordinated farming cluster is still worth surfacing.
"""
import uuid
from datetime import datetime, timedelta, timezone

from sqlalchemy import select

from app.config import get_settings
from app.db.models import Alert, Wallet
from app.db.models.cluster_event import ClusterEvent
from app.db.models.trade import Trade
from app.db.session import get_session_factory
from app.logging import get_logger
from app.schemas.trade import NormalizedTrade
from app.services.alerts import telegram

logger = get_logger(__name__)

_CLUSTER_MIN_WALLETS = 3
_CLUSTER_PRICE_TOLERANCE = 0.02
_CLUSTER_SIZE_RATIO_MAX = 1.20     # max_size / min_size
_CLUSTER_WINDOW_MINUTES = 30
_FARMING_PRICE_CEILING = 0.90
_DEDUPE_PREFIX = "sybil_cluster"


async def _get_redis():
    import redis.asyncio as redis_async
    settings = get_settings()
    return redis_async.from_url(settings.redis_url)


async def _is_deduped(dedupe_key: str) -> bool:
    try:
        r = await _get_redis()
        return bool(await r.exists(f"{_DEDUPE_PREFIX}:{dedupe_key}"))
    except Exception as exc:
        logger.warning("cluster_dedupe_check_error", error=str(exc))
        return False


async def _mark_deduped(dedupe_key: str, cooldown: int) -> None:
    try:
        r = await _get_redis()
        await r.setex(f"{_DEDUPE_PREFIX}:{dedupe_key}", cooldown, "1")
    except Exception as exc:
        logger.warning("cluster_dedupe_mark_error", error=str(exc))


def _cluster_dedupe_key(market_id: str, outcome: str, price: float, ts: datetime) -> str:
    price_bucket = int(price * 100)
    time_bucket = int(ts.timestamp()) // (_CLUSTER_WINDOW_MINUTES * 60)
    return f"{market_id}:{outcome}:{price_bucket}:{time_bucket}"


async def check_sybil_cluster(trade: NormalizedTrade) -> None:
    """
    Check whether the incoming trade is part of a sybil cluster.
    Called after a trade is persisted. No-op if trade has no wallet or outcome.
    """
    if not trade.wallet or not trade.outcome or trade.price is None:
        return

    settings = get_settings()
    price = float(trade.price)
    size = float(trade.size) if trade.size else None
    outcome = (trade.outcome or "").upper()
    market_id = trade.market_id
    ts = trade.ts

    window_start = ts - timedelta(minutes=_CLUSTER_WINDOW_MINUTES)
    price_lo = price - _CLUSTER_PRICE_TOLERANCE
    price_hi = price + _CLUSTER_PRICE_TOLERANCE

    factory = get_session_factory()
    async with factory() as session:
        rows = await session.execute(
            select(Trade.wallet, Trade.price, Trade.size, Trade.ts)
            .where(
                Trade.market_id == market_id,
                Trade.outcome == outcome,
                Trade.wallet.is_not(None),
                Trade.attribution_status == "attributed",
                Trade.ts >= window_start,
                Trade.ts <= ts,
                Trade.price >= price_lo,
                Trade.price <= price_hi,
                Trade.trade_type.is_(None),  # exclude already-tagged
            )
        )
        candidates = rows.all()

    # Distinct wallets only
    seen: dict[str, tuple] = {}
    for row in candidates:
        w = row.wallet
        if w and w not in seen:
            seen[w] = row

    if len(seen) < _CLUSTER_MIN_WALLETS:
        return

    # Size homogeneity check
    sizes = [float(r.size) for r in seen.values() if r.size is not None]
    if len(sizes) >= _CLUSTER_MIN_WALLETS:
        max_size = max(sizes)
        min_size = min(sizes)
        if min_size > 0 and max_size / min_size > _CLUSTER_SIZE_RATIO_MAX:
            return  # sizes too spread out

    wallet_list = list(seen.keys())
    dedupe_key = _cluster_dedupe_key(market_id, outcome, price, ts)

    if await _is_deduped(dedupe_key):
        logger.debug("cluster_deduped", market_id=market_id, outcome=outcome)
        return

    # Compute aggregate notional
    agg_notional = sum(
        float(r.price or 0) * float(r.size or 0) for r in seen.values()
    )

    is_farming = price > _FARMING_PRICE_CEILING
    labels = "[SYBIL_CLUSTER]" + (" [FARMING]" if is_farming else "")

    logger.info(
        "sybil_cluster_detected",
        market_id=market_id,
        outcome=outcome,
        wallets=wallet_list,
        price=price,
        aggregate_notional=agg_notional,
        is_farming=is_farming,
    )

    factory = get_session_factory()
    async with factory() as session:
        # Tag all wallets in the cluster
        for wallet_addr in wallet_list:
            w_result = await session.execute(select(Wallet).where(Wallet.wallet == wallet_addr))
            w = w_result.scalar_one_or_none()
            if w:
                w.suspected_sybil = True

        # Save cluster event
        cluster_event = ClusterEvent(
            id=str(uuid.uuid4()),
            market_id=market_id,
            cluster_key=dedupe_key,
            wallets=wallet_list,
            direction=trade.side,
            aggregate_notional=agg_notional,
            start_ts=min(r.ts for r in seen.values()),
            end_ts=ts,
            confidence=len(wallet_list) / 10.0,  # simple confidence heuristic
        )
        session.add(cluster_event)

        # Persist alert
        message = (
            f"{labels}\n\n"
            f"Market: {market_id}\n"
            f"Outcome: {outcome} @ {price:.3f}\n"
            f"Wallets: {len(wallet_list)} — {', '.join(w[:10] for w in wallet_list)}\n"
            f"Combined notional: ${agg_notional:,.0f}"
        )
        alert = Alert(
            id=str(uuid.uuid4()),
            alert_type="SYBIL_CLUSTER",
            severity="high",
            wallet=None,
            market_id=market_id,
            asset_id=trade.asset_id,
            title=f"SYBIL_CLUSTER — {market_id}",
            message=message,
            payload={
                "wallets": wallet_list,
                "outcome": outcome,
                "price": price,
                "aggregate_notional": agg_notional,
                "is_farming": is_farming,
                "cluster_key": dedupe_key,
            },
            dedupe_key=dedupe_key,
            sent=False,
            sent_ts=None,
        )
        session.add(alert)
        await session.commit()

    # Send Telegram
    sent = await telegram.send_message(message)
    if sent:
        factory = get_session_factory()
        async with factory() as session:
            result = await session.execute(select(Alert).where(Alert.dedupe_key == dedupe_key))
            saved_alert = result.scalar_one_or_none()
            if saved_alert:
                saved_alert.sent = True
                saved_alert.sent_ts = datetime.now(tz=timezone.utc)
                await session.commit()

    await _mark_deduped(dedupe_key, settings.default_alert_cooldown_seconds)
