"""Whale detection and alert rule evaluation."""
from datetime import datetime, timezone

from app.config import Settings
from app.db.models.wallet import Wallet
from app.logging import get_logger
from app.schemas.alert import AlertDecision
from app.schemas.trade import NormalizedTrade

logger = get_logger(__name__)

# Lower threshold for watched wallets (spec default: $3,000)
_WATCHED_LOWER_THRESHOLD_USD = 3_000.0

# Minimum balance increase fraction to count as a material holder add
_MIN_HOLDER_ADD_PCT = 0.10


def _wallet_is_cold_start(wallet: Wallet | None) -> bool:
    """Fewer than 3 resolved markets = cold-start (spec F1)."""
    if wallet is None:
        return True
    return (wallet.resolved_markets_count or 0) < 3


def make_dedupe_key(alert_type: str, trade: NormalizedTrade, cooldown_seconds: int) -> str:
    wallet_part = trade.wallet or "none"
    market_part = trade.market_id or "none"
    ts = trade.ts.timestamp() if trade.ts else datetime.now(tz=timezone.utc).timestamp()
    bucket = int(ts) // cooldown_seconds
    return f"{alert_type}:{wallet_part}:{market_part}:{bucket}"



def _no_alert(dedupe_key: str) -> AlertDecision:
    return AlertDecision(should_alert=False, severity="none", alert_type="NONE", dedupe_key=dedupe_key)


def evaluate_trade(
    trade: NormalizedTrade,
    wallet: Wallet | None,
    settings: Settings,
) -> AlertDecision:
    """
    Evaluate trade-based alert rules against a normalized trade.

    Rules (in priority order):
      WATCH_WALLET    — wallet.watch_status == "watch": always alert, no threshold
                         (unless wallet.alert_digest, see below, or below the
                         ping floor for non-digest watched wallets)
      WATCHLIST_WALLET — watch_status == "high-interest" AND notional >= $3,000
      LARGE_TRADE     — notional >= DEFAULT_WHALE_NOTIONAL_USD

    Cold-start wallets (< 3 resolved markets) still trigger LARGE_TRADE
    per spec section F1, labeled "unscored (new)".

    Buy-only routing (decisions/2026-07-19.md item 1a): sell-side trades never
    alert here — they are still ingested into `trades` normally by the
    normalizer, this only suppresses the derived notification, so future
    follow-exit logic that needs sell events keeps seeing them. CRIT system
    alerts (app/services/alerts/system_alerts.py) are a separate code path,
    unaffected by this function entirely.
    """
    if (trade.side or "").upper() == "SELL":
        return _no_alert("sell_suppressed")

    cooldown = settings.default_alert_cooldown_seconds
    whale_threshold = settings.default_whale_notional_usd
    is_cold_start = _wallet_is_cold_start(wallet)
    notional = trade.notional_float

    # WATCH_WALLET: fires for all trades from explicitly watched wallets,
    # regardless of notional size or score. Checked before the notional guard.
    if wallet is not None and wallet.watch_status == "watch":
        is_digest = bool(wallet.alert_digest)

        # Minimum-notional floor for real-time pings (decisions/2026-07-19.md
        # item 1b, option 1 from 2026-07-18.md): only applies to non-digest
        # wallets — digest wallets keep full visibility since they're batched,
        # not streamed, so dust trades don't cost a Telegram message each.
        if not is_digest and notional is not None and notional < settings.watch_wallet_ping_floor_usd:
            return _no_alert("below_ping_floor")

        reasons: list[str] = []
        if notional is not None:
            reasons.append(f"Watched wallet — ${notional:,.0f} notional")
        else:
            reasons.append("Watched wallet")
        payload: dict = {
            "notional_usd": notional,
            "price": float(trade.price) if trade.price else None,
            "side": trade.side,
            "outcome": trade.outcome,
            "asset_id": trade.asset_id,
            "wallet_score": wallet.wallet_score,
            "score_confidence": wallet.score_confidence,
            "is_cold_start": is_cold_start,
            "is_watched": True,
            "is_digest": is_digest,
        }
        logger.debug(
            "alert_rule_matched",
            alert_type="WATCH_WALLET",
            severity="watch",
            notional_usd=notional,
            wallet=trade.wallet,
            market_id=trade.market_id,
            is_cold_start=is_cold_start,
            is_digest=is_digest,
        )
        return AlertDecision(
            should_alert=True,
            severity="watch",
            alert_type="WATCH_WALLET",
            dedupe_key=make_dedupe_key("WATCH_WALLET", trade, cooldown),
            reasons=reasons,
            payload=payload,
        )

    if notional is None:
        return AlertDecision(
            should_alert=False,
            severity="none",
            alert_type="NONE",
            dedupe_key="no_notional",
        )

    is_high_interest = wallet is not None and wallet.watch_status == "high-interest"

    reasons = []
    alert_type: str | None = None
    severity = "low"

    # WATCHLIST_WALLET: high-interest wallets above lower threshold
    if is_high_interest and notional >= _WATCHED_LOWER_THRESHOLD_USD:
        alert_type = "WATCHLIST_WALLET"
        severity = "medium"
        reasons.append(f"Watched wallet — ${notional:,.0f} notional")

    # LARGE_TRADE: whale-threshold rule
    if notional >= whale_threshold:
        if alert_type is None:
            alert_type = "LARGE_TRADE"
            severity = "low"
        reasons.append(f"${notional:,.0f} exceeds whale threshold ${whale_threshold:,.0f}")

    if alert_type is None:
        return AlertDecision(
            should_alert=False,
            severity="none",
            alert_type="NONE",
            dedupe_key=make_dedupe_key("NONE", trade, cooldown),
        )

    payload = {
        "notional_usd": notional,
        "price": float(trade.price) if trade.price else None,
        "side": trade.side,
        "outcome": trade.outcome,
        "asset_id": trade.asset_id,
        "wallet_score": wallet.wallet_score if wallet else 0.0,
        "score_confidence": wallet.score_confidence if wallet else "low",
        "is_cold_start": is_cold_start,
        "is_watched": is_high_interest,
    }

    logger.debug(
        "alert_rule_matched",
        alert_type=alert_type,
        severity=severity,
        notional_usd=notional,
        wallet=trade.wallet,
        market_id=trade.market_id,
        is_cold_start=is_cold_start,
    )

    return AlertDecision(
        should_alert=True,
        severity=severity,
        alert_type=alert_type,
        dedupe_key=make_dedupe_key(alert_type, trade, cooldown),
        reasons=reasons,
        payload=payload,
    )


