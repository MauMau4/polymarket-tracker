"""Telegram alert message formatter."""
from datetime import datetime

from app.db.models.wallet import Wallet
from app.schemas.alert import AlertDecision
from app.schemas.trade import NormalizedTrade

_SEVERITY_HEADERS = {
    "high": "[HIGH SIGNAL]",
    "medium": "[MEDIUM SIGNAL]",
    "low": "[LARGE TRADE]",
    "watch": "[WATCH WALLET]",
}


def _short_wallet(address: str | None) -> str:
    if not address:
        return "Unknown"
    if len(address) <= 10:
        return address
    return f"{address[:6]}...{address[-4:]}"


def _action_str(trade: NormalizedTrade) -> str:
    parts: list[str] = []
    if trade.notional_usd:
        parts.append(f"${float(trade.notional_usd):,.0f}")
    if trade.outcome:
        parts.append(trade.outcome.upper())
    if trade.price:
        parts.append(f"@ {float(trade.price):.3f}")
    action = " ".join(parts) if parts else "trade"
    if trade.side:
        action += f" — {'Buy' if trade.side.upper() == 'BUY' else 'Sell'}"
    return action


def _wallet_score_line(wallet: Wallet | None) -> str:
    if wallet is None or (wallet.resolved_markets_count or 0) < 3:
        return "Score: unscored (new wallet)"
    return f"Score: {wallet.wallet_score:.1f} ({wallet.score_confidence} confidence)"


def format_alert(
    decision: AlertDecision,
    trade: NormalizedTrade,
    wallet: Wallet | None,
    market_question: str | None,
    market_end_date: datetime | None = None,
) -> str:
    """
    Format a trade-based alert decision as a Telegram message.

    Example output:
      [HIGH SIGNAL]

      Wallet: 0x1234...abcd
      Score: 84.2 (high confidence)
      Action: $22,400 YES @ 0.410 — Buy
      Market: Will X happen by June?
      Ends: May 15, 2026
      Context:
      - Large trade threshold exceeded
    """
    header = _SEVERITY_HEADERS.get(decision.severity, "[ALERT]")
    wallet_line = f"Wallet: {_short_wallet(trade.wallet)}"
    score_line = _wallet_score_line(wallet)
    action_line = f"Action: {_action_str(trade)}"
    market_line = f"Market: {market_question or trade.market_id or 'Unknown'}"

    context_items: list[str] = list(decision.reasons)
    if decision.payload.get("is_cold_start"):
        if "Wallet: unscored (new)" not in context_items:
            context_items.append("Wallet: unscored (new)")

    context_block = "Context:\n" + "\n".join(f"- {r}" for r in context_items) if context_items else ""

    lines = [header, "", wallet_line, score_line, action_line, market_line]
    if market_end_date is not None:
        ends_str = f"{market_end_date.strftime('%b')} {market_end_date.day}, {market_end_date.strftime('%Y')}"
        lines.append(f"Ends: {ends_str}")
    if context_block:
        lines.append(context_block)

    return "\n".join(lines)


