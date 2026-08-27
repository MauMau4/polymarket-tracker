"""Unit tests for alert message formatter."""
from datetime import datetime, timezone
from decimal import Decimal
from unittest.mock import MagicMock

import pytest

from app.schemas.alert import AlertDecision
from app.schemas.trade import NormalizedTrade
from app.services.alerts.formatter import format_alert, _short_wallet


def _make_trade(
    notional: float = 22_400.0,
    price: float = 0.41,
    wallet: str = "0x1234567890abcdef1234567890abcdef12345678",
    outcome: str = "YES",
    market_id: str = "0xMarket",
    side: str | None = None,
) -> NormalizedTrade:
    return NormalizedTrade(
        ts=datetime(2024, 1, 1, 12, 0, 0, tzinfo=timezone.utc),
        market_id=market_id,
        asset_id="0xAsset",
        notional_usd=Decimal(str(notional)),
        price=Decimal(str(price)),
        size=Decimal("54878.05"),
        outcome=outcome,
        side=side,
        wallet=wallet,
        attribution_status="attributed",
        source="ws_market",
    )


def _make_decision(
    alert_type: str = "LARGE_TRADE",
    severity: str = "low",
    reasons: list[str] | None = None,
    payload: dict | None = None,
) -> AlertDecision:
    return AlertDecision(
        should_alert=True,
        severity=severity,
        alert_type=alert_type,
        dedupe_key="test-key",
        reasons=reasons or ["Notional $22,400 exceeds whale threshold $10,000"],
        payload=payload or {"is_cold_start": True, "is_watched": False},
    )


def _make_wallet(resolved_count: int = 0, score: float = 0.0, confidence: str = "low"):
    w = MagicMock()
    w.resolved_markets_count = resolved_count
    w.wallet_score = score
    w.score_confidence = confidence
    return w


class TestShortWallet:
    def test_long_address_truncated(self):
        addr = "0x1234567890abcdef1234567890abcdef12345678"
        assert _short_wallet(addr) == "0x1234...5678"

    def test_none_returns_unknown(self):
        assert _short_wallet(None) == "Unknown"

    def test_short_address_returned_as_is(self):
        assert _short_wallet("0xABCD") == "0xABCD"


class TestFormatAlert:
    def test_large_trade_contains_header(self):
        msg = format_alert(_make_decision(), _make_trade(), None, "Will X happen?")
        assert "[LARGE TRADE]" in msg

    def test_high_severity_header(self):
        decision = _make_decision(severity="high")
        msg = format_alert(decision, _make_trade(), None, "Market Q")
        assert "[HIGH SIGNAL]" in msg

    def test_medium_severity_header(self):
        decision = _make_decision(severity="medium", alert_type="WATCHLIST_WALLET")
        msg = format_alert(decision, _make_trade(), None, "Market Q")
        assert "[MEDIUM SIGNAL]" in msg

    def test_wallet_address_truncated_in_message(self):
        msg = format_alert(_make_decision(), _make_trade(), None, "Market Q")
        assert "0x1234...5678" in msg

    def test_market_question_present(self):
        msg = format_alert(_make_decision(), _make_trade(), None, "Will the S&P close above 5000?")
        assert "Will the S&P close above 5000?" in msg

    def test_market_id_fallback_when_no_question(self):
        msg = format_alert(_make_decision(), _make_trade(), None, None)
        assert "0xMarket" in msg

    def test_cold_start_label_present(self):
        decision = _make_decision(payload={"is_cold_start": True, "is_watched": False})
        msg = format_alert(decision, _make_trade(), None, "Market Q")
        assert "unscored" in msg.lower()

    def test_scored_wallet_shows_score(self):
        wallet = _make_wallet(resolved_count=5, score=72.3, confidence="high")
        decision = _make_decision(payload={"is_cold_start": False, "is_watched": False})
        msg = format_alert(decision, _make_trade(), wallet, "Market Q")
        assert "72.3" in msg
        assert "high" in msg

    def test_unscored_wallet_shows_unscored(self):
        wallet = _make_wallet(resolved_count=2, score=0.0, confidence="low")
        msg = format_alert(_make_decision(), _make_trade(), wallet, "Market Q")
        assert "unscored" in msg.lower()

    def test_action_includes_notional_and_outcome(self):
        msg = format_alert(_make_decision(), _make_trade(notional=22_400.0, outcome="YES"), None, "Q")
        assert "$22,400" in msg
        assert "YES" in msg

    def test_context_reasons_present(self):
        decision = _make_decision(reasons=["Notional threshold exceeded", "Watched wallet"])
        msg = format_alert(decision, _make_trade(), None, "Market Q")
        assert "Notional threshold exceeded" in msg
        assert "Watched wallet" in msg

    def test_no_wallet_shows_unknown(self):
        trade = NormalizedTrade(
            ts=datetime(2024, 1, 1, tzinfo=timezone.utc),
            market_id="0xMarket",
            asset_id="0xAsset",
            notional_usd=Decimal("15000"),
            source="ws_market",
        )
        msg = format_alert(_make_decision(), trade, None, "Market Q")
        assert "Unknown" in msg

    def test_action_line_includes_buy_direction(self):
        msg = format_alert(_make_decision(), _make_trade(side="BUY"), None, "Market Q")
        assert "— Buy" in msg

    def test_action_line_includes_sell_direction(self):
        msg = format_alert(_make_decision(), _make_trade(side="SELL"), None, "Market Q")
        assert "— Sell" in msg

    def test_action_line_no_direction_when_side_missing(self):
        msg = format_alert(_make_decision(), _make_trade(side=None), None, "Market Q")
        assert "— Buy" not in msg
        assert "— Sell" not in msg
