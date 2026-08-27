"""Unit tests for whale detection and alert rule evaluation."""
from datetime import datetime, timezone
from decimal import Decimal
from unittest.mock import MagicMock

import pytest

from app.schemas.trade import NormalizedTrade
from app.services.alerts.rules import evaluate_trade, make_dedupe_key


def _make_settings(whale_threshold=10_000.0, cooldown=900, ping_floor=20.0):
    s = MagicMock()
    s.default_whale_notional_usd = whale_threshold
    s.default_alert_cooldown_seconds = cooldown
    s.watch_wallet_ping_floor_usd = ping_floor
    return s


def _make_trade(
    notional: float | None, wallet: str | None = "0xABCD", side: str | None = "BUY"
) -> NormalizedTrade:
    return NormalizedTrade(
        ts=datetime(2024, 1, 1, 12, 0, 0, tzinfo=timezone.utc),
        market_id="0xMarket",
        asset_id="0xAsset",
        notional_usd=Decimal(str(notional)) if notional is not None else None,
        price=Decimal("0.50"),
        size=Decimal(str(notional * 2)) if notional is not None else None,
        side=side,
        wallet=wallet,
        attribution_status="attributed" if wallet else "unresolved",
        source="ws_market",
    )


def _make_wallet(
    watch_status: str | None = None,
    resolved_count: int = 0,
    score: float = 0.0,
    alert_digest: bool = False,
):
    w = MagicMock()
    w.watch_status = watch_status
    w.resolved_markets_count = resolved_count
    w.wallet_score = score
    w.score_confidence = "low" if resolved_count < 3 else "high"
    w.alert_digest = alert_digest
    return w


class TestLargeTradeRule:
    def test_fires_above_threshold(self):
        trade = _make_trade(15_000.0)
        decision = evaluate_trade(trade, None, _make_settings())
        assert decision.should_alert is True
        assert decision.alert_type == "LARGE_TRADE"
        assert decision.severity == "low"

    def test_no_alert_below_threshold(self):
        trade = _make_trade(9_999.0)
        decision = evaluate_trade(trade, None, _make_settings())
        assert decision.should_alert is False

    def test_exactly_at_threshold_fires(self):
        trade = _make_trade(10_000.0)
        decision = evaluate_trade(trade, None, _make_settings())
        assert decision.should_alert is True

    def test_no_notional_no_alert(self):
        trade = _make_trade(None)
        decision = evaluate_trade(trade, None, _make_settings())
        assert decision.should_alert is False

    def test_unattributed_wallet_still_fires(self):
        trade = _make_trade(20_000.0, wallet=None)
        decision = evaluate_trade(trade, None, _make_settings())
        assert decision.should_alert is True
        assert decision.alert_type == "LARGE_TRADE"


class TestColdStartBehavior:
    def test_cold_start_wallet_fires_large_trade(self):
        """Spec F1: cold-start wallets still trigger LARGE_TRADE with severity low/medium."""
        trade = _make_trade(15_000.0)
        wallet = _make_wallet(resolved_count=0)
        decision = evaluate_trade(trade, wallet, _make_settings())
        assert decision.should_alert is True
        assert decision.alert_type == "LARGE_TRADE"
        assert decision.payload["is_cold_start"] is True

    def test_wallet_with_3_markets_not_cold_start(self):
        trade = _make_trade(15_000.0)
        wallet = _make_wallet(resolved_count=3)
        decision = evaluate_trade(trade, wallet, _make_settings())
        assert decision.payload["is_cold_start"] is False

    def test_wallet_with_2_markets_is_cold_start(self):
        trade = _make_trade(15_000.0)
        wallet = _make_wallet(resolved_count=2)
        decision = evaluate_trade(trade, wallet, _make_settings())
        assert decision.payload["is_cold_start"] is True


class TestWatchWalletRule:
    """watch_status='watch' fires WATCH_WALLET on every trade, no notional threshold."""

    def test_fires_above_lower_threshold(self):
        trade = _make_trade(5_000.0)
        wallet = _make_wallet(watch_status="watch")
        decision = evaluate_trade(trade, wallet, _make_settings())
        assert decision.should_alert is True
        assert decision.alert_type == "WATCH_WALLET"
        assert decision.severity == "watch"

    def test_fires_below_lower_threshold(self):
        """No notional floor — any trade from a watched wallet triggers."""
        trade = _make_trade(1_000.0)
        wallet = _make_wallet(watch_status="watch")
        decision = evaluate_trade(trade, wallet, _make_settings())
        assert decision.should_alert is True
        assert decision.alert_type == "WATCH_WALLET"

    def test_fires_above_whale_threshold(self):
        """Watched wallet above whale threshold → WATCH_WALLET takes priority."""
        trade = _make_trade(15_000.0)
        wallet = _make_wallet(watch_status="watch")
        decision = evaluate_trade(trade, wallet, _make_settings())
        assert decision.alert_type == "WATCH_WALLET"
        assert decision.payload["is_watched"] is True

    def test_zero_notional_below_ping_floor_suppressed(self):
        """decisions/2026-07-19.md item 1b superseded the old no-floor
        behavior: a non-digest watched wallet's dust trade no longer pings."""
        trade = _make_trade(0.01)
        wallet = _make_wallet(watch_status="watch")
        decision = evaluate_trade(trade, wallet, _make_settings())
        assert decision.should_alert is False

    def test_zero_notional_fires_for_digest_wallet(self):
        """Digest wallets keep full visibility — no ping floor applies."""
        trade = _make_trade(0.01)
        wallet = _make_wallet(watch_status="watch", alert_digest=True)
        decision = evaluate_trade(trade, wallet, _make_settings())
        assert decision.should_alert is True
        assert decision.alert_type == "WATCH_WALLET"

    def test_independent_of_score(self):
        """Fires regardless of wallet score (cold-start or scored)."""
        trade = _make_trade(500.0)
        wallet_new = _make_wallet(watch_status="watch", resolved_count=0)
        wallet_scored = _make_wallet(watch_status="watch", resolved_count=10, score=80.0)
        d1 = evaluate_trade(trade, wallet_new, _make_settings())
        d2 = evaluate_trade(trade, wallet_scored, _make_settings())
        assert d1.alert_type == "WATCH_WALLET"
        assert d2.alert_type == "WATCH_WALLET"


class TestWatchlistWalletRule:
    def test_high_interest_wallet_triggers_with_threshold(self):
        trade = _make_trade(5_000.0)
        wallet = _make_wallet(watch_status="high-interest")
        decision = evaluate_trade(trade, wallet, _make_settings())
        assert decision.should_alert is True
        assert decision.alert_type == "WATCHLIST_WALLET"
        assert decision.severity == "medium"

    def test_high_interest_wallet_no_alert_below_threshold(self):
        trade = _make_trade(1_000.0)
        wallet = _make_wallet(watch_status="high-interest")
        decision = evaluate_trade(trade, wallet, _make_settings())
        assert decision.should_alert is False

    def test_ignored_status_wallet_does_not_trigger_watchlist(self):
        trade = _make_trade(5_000.0)
        wallet = _make_wallet(watch_status="ignore")
        decision = evaluate_trade(trade, wallet, _make_settings())
        assert decision.alert_type != "WATCHLIST_WALLET"
        assert decision.alert_type != "WATCH_WALLET"


class TestBuyOnlySuppression:
    """decisions/2026-07-19.md item 1a: sell-side trades never alert."""

    def test_sell_large_trade_suppressed(self):
        trade = _make_trade(15_000.0, side="SELL")
        decision = evaluate_trade(trade, None, _make_settings())
        assert decision.should_alert is False

    def test_sell_watch_wallet_suppressed(self):
        trade = _make_trade(5_000.0, side="SELL")
        wallet = _make_wallet(watch_status="watch")
        decision = evaluate_trade(trade, wallet, _make_settings())
        assert decision.should_alert is False

    def test_sell_watchlist_wallet_suppressed(self):
        trade = _make_trade(5_000.0, side="SELL")
        wallet = _make_wallet(watch_status="high-interest")
        decision = evaluate_trade(trade, wallet, _make_settings())
        assert decision.should_alert is False

    def test_sell_case_insensitive(self):
        trade = _make_trade(15_000.0, side="sell")
        decision = evaluate_trade(trade, None, _make_settings())
        assert decision.should_alert is False

    def test_buy_unaffected(self):
        trade = _make_trade(15_000.0, side="BUY")
        decision = evaluate_trade(trade, None, _make_settings())
        assert decision.should_alert is True


class TestWatchWalletPingFloor:
    """decisions/2026-07-19.md item 1b: non-digest watched wallets get a
    minimum-notional floor on real-time pings; digest wallets don't."""

    def test_below_floor_suppressed_for_non_digest(self):
        trade = _make_trade(10.0)
        wallet = _make_wallet(watch_status="watch", alert_digest=False)
        decision = evaluate_trade(trade, wallet, _make_settings(ping_floor=20.0))
        assert decision.should_alert is False

    def test_at_floor_fires_for_non_digest(self):
        trade = _make_trade(20.0)
        wallet = _make_wallet(watch_status="watch", alert_digest=False)
        decision = evaluate_trade(trade, wallet, _make_settings(ping_floor=20.0))
        assert decision.should_alert is True

    def test_below_floor_still_fires_for_digest_wallet(self):
        trade = _make_trade(1.0)
        wallet = _make_wallet(watch_status="watch", alert_digest=True)
        decision = evaluate_trade(trade, wallet, _make_settings(ping_floor=20.0))
        assert decision.should_alert is True
        assert decision.payload["is_digest"] is True

    def test_non_digest_payload_flag_false(self):
        trade = _make_trade(5_000.0)
        wallet = _make_wallet(watch_status="watch", alert_digest=False)
        decision = evaluate_trade(trade, wallet, _make_settings())
        assert decision.payload["is_digest"] is False


class TestDedupeKey:
    def test_same_inputs_same_key(self):
        trade = _make_trade(15_000.0)
        k1 = make_dedupe_key("LARGE_TRADE", trade, 900)
        k2 = make_dedupe_key("LARGE_TRADE", trade, 900)
        assert k1 == k2

    def test_different_market_different_key(self):
        t1 = _make_trade(15_000.0)
        t2 = NormalizedTrade(
            ts=t1.ts,
            market_id="0xOtherMarket",
            asset_id="0xAsset",
            notional_usd=Decimal("15000"),
            source="ws_market",
        )
        k1 = make_dedupe_key("LARGE_TRADE", t1, 900)
        k2 = make_dedupe_key("LARGE_TRADE", t2, 900)
        assert k1 != k2

    def test_different_time_bucket_different_key(self):
        t1 = NormalizedTrade(
            ts=datetime(2024, 1, 1, 12, 0, 0, tzinfo=timezone.utc),
            market_id="0xMarket",
            asset_id="0xAsset",
            source="ws_market",
        )
        t2 = NormalizedTrade(
            ts=datetime(2024, 1, 1, 12, 20, 0, tzinfo=timezone.utc),  # 20 min later = new bucket
            market_id="0xMarket",
            asset_id="0xAsset",
            source="ws_market",
        )
        k1 = make_dedupe_key("LARGE_TRADE", t1, 900)
        k2 = make_dedupe_key("LARGE_TRADE", t2, 900)
        assert k1 != k2

    def test_same_bucket_window_same_key(self):
        """Two trades 5 min apart within the same 15-min bucket produce the same key."""
        t1 = NormalizedTrade(
            ts=datetime(2024, 1, 1, 12, 0, 0, tzinfo=timezone.utc),
            market_id="0xMarket",
            asset_id="0xAsset",
            wallet="0xWallet",
            source="ws_market",
        )
        t2 = NormalizedTrade(
            ts=datetime(2024, 1, 1, 12, 5, 0, tzinfo=timezone.utc),  # 5 min later, same bucket
            market_id="0xMarket",
            asset_id="0xAsset",
            wallet="0xWallet",
            source="ws_market",
        )
        k1 = make_dedupe_key("LARGE_TRADE", t1, 900)
        k2 = make_dedupe_key("LARGE_TRADE", t2, 900)
        assert k1 == k2
