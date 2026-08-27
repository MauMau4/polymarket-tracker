"""Unit tests for the trade normalizer and WS event parsing."""
import pytest
from decimal import Decimal
from datetime import datetime, timedelta, timezone

from app.services.polymarket.websocket_client import parse_events
from app.services.polymarket.normalizer import (
    TradeNormalizer,
    _extract_trade_events,
    _build_normalized_trade,
    _finalize_attribution,
)
from app.services.polymarket.clob_client import ClobTrade
from tests.fixtures.payloads import (
    WS_LAST_TRADE_PRICE,
    WS_PRICE_CHANGE,
    WS_BOOK_EVENT,
    CLOB_TRADES_RESPONSE,
    ASSET_ID,
    MARKET_ID,
    WALLET_ADDRESS,
    TX_HASH,
)


class TestParseEvents:
    def test_single_dict_returned_as_list(self):
        result = parse_events(WS_LAST_TRADE_PRICE)
        assert result == [WS_LAST_TRADE_PRICE]

    def test_list_of_dicts_returned_flat(self):
        result = parse_events([WS_LAST_TRADE_PRICE, WS_BOOK_EVENT])
        assert len(result) == 2

    def test_empty_list(self):
        result = parse_events([])
        assert result == []


class TestExtractTradeEvents:
    def test_last_trade_price_extracted(self):
        events = _extract_trade_events(WS_LAST_TRADE_PRICE)
        assert len(events) == 1
        assert events[0]["event_type"] == "last_trade_price"

    def test_price_change_ignored(self):
        # price_change events are order-book level changes, not fills —
        # they must never become trades.
        events = _extract_trade_events(WS_PRICE_CHANGE)
        assert events == []

    def test_book_event_ignored(self):
        events = _extract_trade_events(WS_BOOK_EVENT)
        assert events == []

    def test_unknown_event_type_ignored(self):
        events = _extract_trade_events({"event_type": "completely_unknown_type", "asset_id": ASSET_ID})
        assert events == []


class TestBuildNormalizedTrade:
    def _make_clob_trade(self) -> ClobTrade:
        return ClobTrade(CLOB_TRADES_RESPONSE["data"][0])

    def test_attributed_trade_built_correctly(self):
        clob = self._make_clob_trade()
        trade = _build_normalized_trade(
            event=WS_LAST_TRADE_PRICE,
            asset_id=ASSET_ID,
            market_id=MARKET_ID,
            wallet=WALLET_ADDRESS,
            tx_hash=TX_HASH,
            attribution_status="attributed",
            clob_match=clob,
            outcome="Yes",  # resolved from tokens table by caller, not from clob_match
        )
        assert trade.asset_id == ASSET_ID
        assert trade.market_id == MARKET_ID
        assert trade.wallet == WALLET_ADDRESS
        assert trade.tx_hash == TX_HASH
        assert trade.attribution_status == "attributed"
        assert trade.price == Decimal("0.41")
        assert trade.size == Decimal("54878.05")
        assert trade.notional_usd is not None
        assert float(trade.notional_usd) == pytest.approx(22500.0, abs=1.0)
        assert trade.outcome == "Yes"
        assert trade.source == "ws_market"

    def test_outcome_not_taken_from_clob_match(self):
        """clob_match.outcome is ignored; only the explicit outcome param is used."""
        clob = self._make_clob_trade()  # clob has outcome="Yes" in fixture
        trade = _build_normalized_trade(
            event=WS_LAST_TRADE_PRICE,
            asset_id=ASSET_ID,
            market_id=MARKET_ID,
            wallet=WALLET_ADDRESS,
            tx_hash=TX_HASH,
            attribution_status="attributed",
            clob_match=clob,
            outcome="No",  # tokens table says "No" for this asset
        )
        assert trade.outcome == "No"  # tokens table wins, not clob_match

    def test_outcome_none_when_not_in_map(self):
        """Outcome is None when the asset is not in the tokens map."""
        clob = self._make_clob_trade()
        trade = _build_normalized_trade(
            event=WS_LAST_TRADE_PRICE,
            asset_id=ASSET_ID,
            market_id=MARKET_ID,
            wallet=WALLET_ADDRESS,
            tx_hash=TX_HASH,
            attribution_status="attributed",
            clob_match=clob,
            outcome=None,
        )
        assert trade.outcome is None

    def test_unresolved_trade_wallet_is_none(self):
        trade = _build_normalized_trade(
            event=WS_LAST_TRADE_PRICE,
            asset_id=ASSET_ID,
            market_id=MARKET_ID,
            wallet=None,
            tx_hash=None,
            attribution_status="unresolved",
            clob_match=None,
        )
        assert trade.wallet is None
        assert trade.attribution_status == "unresolved"
        assert trade.price == Decimal("0.41")

    def test_notional_computed_from_price_and_size(self):
        trade = _build_normalized_trade(
            event=WS_LAST_TRADE_PRICE,
            asset_id=ASSET_ID,
            market_id=MARKET_ID,
            wallet=None,
            tx_hash=None,
            attribution_status="unresolved",
            clob_match=None,
        )
        assert trade.notional_usd == (Decimal("0.41") * Decimal("54878.05")).quantize(Decimal("0.01"))

    def test_timestamp_parsed_from_event(self):
        trade = _build_normalized_trade(
            event=WS_LAST_TRADE_PRICE,
            asset_id=ASSET_ID,
            market_id=MARKET_ID,
            wallet=None,
            tx_hash=None,
            attribution_status="unresolved",
            clob_match=None,
        )
        assert trade.ts.tzinfo is not None
        assert trade.ts == datetime.fromtimestamp(1704067200.123456, tz=timezone.utc)


class TestClobTrade:
    def test_accessors(self):
        raw = CLOB_TRADES_RESPONSE["data"][0]
        ct = ClobTrade(raw)
        assert ct.trade_id == "trade-id-001"
        assert ct.asset_id == ASSET_ID
        assert ct.maker_address == WALLET_ADDRESS
        assert ct.transaction_hash == TX_HASH
        assert ct.price == pytest.approx(0.41)
        assert ct.size == pytest.approx(54878.05)
        assert ct.outcome == "Yes"
        assert ct.match_time is not None

    def test_match_time_is_utc(self):
        raw = CLOB_TRADES_RESPONSE["data"][0]
        ct = ClobTrade(raw)
        assert ct.match_time.tzinfo is not None


class TestShouldFilter:
    """Tests for the notional minimum filter — the only remaining ingestion filter.

    Price ceiling and days-to-resolution filters have been removed. Only notional
    size is checked, and only for non-watched wallets (bypass handled upstream).
    """

    def _n(self):
        return TradeNormalizer()

    def test_below_threshold_is_filtered(self):
        assert self._n()._should_filter(5.0)

    def test_at_threshold_not_filtered(self):
        """Exactly at the minimum (20.0) is not below it — must pass."""
        assert not self._n()._should_filter(20.0)

    def test_above_threshold_not_filtered(self):
        assert not self._n()._should_filter(1000.0)

    def test_none_notional_not_filtered(self):
        """Unknown notional (size missing from event) must not be dropped."""
        assert not self._n()._should_filter(None)

    def test_zero_notional_is_filtered(self):
        assert self._n()._should_filter(0.0)

    def test_just_below_threshold_is_filtered(self):
        assert self._n()._should_filter(19.99)

    def test_high_price_large_notional_not_filtered(self):
        """Price > 0.90 is no longer a filter criterion — only notional matters."""
        assert not self._n()._should_filter(5000.0)

    def test_long_dated_large_notional_not_filtered(self):
        """60-day ceiling is removed — only notional matters."""
        assert not self._n()._should_filter(100.0)

    def test_one_cent_above_threshold_not_filtered(self):
        assert not self._n()._should_filter(20.01)


class TestFinalizeAttribution:
    """
    decisions/2026-08-11.md, item 0c: find_matching_trade()'s Data API query
    is unscoped by asset/time, so its raw result is a price-coincidence
    guess, not real attribution. Only a watched-wallet candidate that also
    passes verify_wallet_attribution's wallet-scoped confirmation may be
    written — everything else must come back NULL/unresolved, never a
    guessed wallet.
    """

    def test_no_candidate_wallet_stays_unresolved(self):
        wallet, tx_hash, status = _finalize_attribution(
            wallet=None, tx_hash=None, attribution_status="unresolved",
            is_watched=False, verification_confirmed=False,
        )
        assert wallet is None
        assert tx_hash is None
        assert status == "unresolved"

    def test_unwatched_candidate_is_discarded_not_guessed(self):
        """The common case: a price-coincidence match for a wallet that
        isn't watched has no independent check at all and must never be
        written as the trade's wallet."""
        wallet, tx_hash, status = _finalize_attribution(
            wallet=WALLET_ADDRESS, tx_hash=TX_HASH, attribution_status="attributed",
            is_watched=False, verification_confirmed=False,
        )
        assert wallet is None
        assert tx_hash is None
        assert status == "unresolved"

    def test_watched_candidate_unconfirmed_is_discarded(self):
        wallet, tx_hash, status = _finalize_attribution(
            wallet=WALLET_ADDRESS, tx_hash=TX_HASH, attribution_status="attributed",
            is_watched=True, verification_confirmed=False,
        )
        assert wallet is None
        assert tx_hash is None
        assert status == "unresolved"

    def test_watched_candidate_confirmed_is_trusted(self):
        """The one surviving path: verify_wallet_attribution still works
        where it genuinely confirms."""
        wallet, tx_hash, status = _finalize_attribution(
            wallet=WALLET_ADDRESS, tx_hash=TX_HASH, attribution_status="attributed",
            is_watched=True, verification_confirmed=True,
        )
        assert wallet == WALLET_ADDRESS
        assert tx_hash == TX_HASH
        assert status == "attributed"


class TestWatchedWallets:
    """Tests for the watched-wallet in-memory set."""

    def test_load_watched_wallets_stores_set(self):
        n = TradeNormalizer()
        wallets = {"0xABC", "0xDEF"}
        n.load_watched_wallets(wallets)
        assert n._watched_wallets == {"0xabc", "0xdef"}

    def test_watched_wallets_starts_empty(self):
        n = TradeNormalizer()
        assert n._watched_wallets == set()

    def test_load_watched_wallets_replaces_previous(self):
        n = TradeNormalizer()
        n.load_watched_wallets({"0xOLD"})
        n.load_watched_wallets({"0xNEW1", "0xNEW2"})
        assert "0xold" not in n._watched_wallets
        assert "0xnew1" in n._watched_wallets
