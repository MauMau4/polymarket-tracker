"""Unit tests for outcome label resolution and selection display formatting."""
import pytest

from app.services.polymarket.normalizer import TradeNormalizer
from app.ui.views import _fmt_selection


# ---------------------------------------------------------------------------
# _fmt_selection: display formatting
# ---------------------------------------------------------------------------

class TestFmtSelection:
    """Selection column shows trade.outcome as-is with no suffix appended."""

    def test_binary_yes_shown_directly(self):
        assert _fmt_selection("Yes", "BUY") == "Yes"

    def test_binary_no_shown_directly(self):
        assert _fmt_selection("No", "BUY") == "No"

    def test_binary_yes_sell_still_shown_directly(self):
        assert _fmt_selection("No", "SELL") == "No"

    def test_categorical_buy_returns_outcome_only(self):
        assert _fmt_selection("BJP", "BUY") == "BJP"

    def test_categorical_sell_returns_outcome_only(self):
        assert _fmt_selection("BJP", "SELL") == "BJP"

    def test_categorical_over_buy(self):
        assert _fmt_selection("Over", "BUY") == "Over"

    def test_categorical_under_sell(self):
        assert _fmt_selection("Under", "SELL") == "Under"

    def test_categorical_team_name_buy(self):
        assert _fmt_selection("Lorenzo Musetti", "BUY") == "Lorenzo Musetti"

    def test_categorical_team_name_sell(self):
        assert _fmt_selection("T1", "SELL") == "T1"

    def test_side_ignored(self):
        assert _fmt_selection("BJP", "buy") == "BJP"
        assert _fmt_selection("BJP", "sell") == "BJP"

    def test_none_outcome_returns_dash(self):
        assert _fmt_selection(None, "BUY") == "—"
        assert _fmt_selection(None, None) == "—"

    def test_categorical_no_side_returns_outcome_only(self):
        assert _fmt_selection("BJP", None) == "BJP"
        assert _fmt_selection("BJP", "") == "BJP"


# ---------------------------------------------------------------------------
# TradeNormalizer: outcome map management
# ---------------------------------------------------------------------------

class TestTradeNormalizerOutcomeMap:
    def _make_normalizer(self) -> TradeNormalizer:
        return TradeNormalizer()

    def test_update_asset_outcome_stores_value(self):
        n = self._make_normalizer()
        n.update_asset_outcome("asset-1", "Yes")
        assert n._asset_to_outcome["asset-1"] == "Yes"

    def test_update_asset_outcome_stores_none(self):
        n = self._make_normalizer()
        n.update_asset_outcome("asset-1", None)
        assert n._asset_to_outcome["asset-1"] is None

    def test_load_asset_outcome_map_bulk_loads(self):
        n = self._make_normalizer()
        mapping = {"asset-1": "Yes", "asset-2": "No", "asset-3": "BJP"}
        n.load_asset_outcome_map(mapping)
        assert n._asset_to_outcome["asset-1"] == "Yes"
        assert n._asset_to_outcome["asset-2"] == "No"
        assert n._asset_to_outcome["asset-3"] == "BJP"

    def test_load_asset_outcome_map_is_additive(self):
        n = self._make_normalizer()
        n.update_asset_outcome("asset-existing", "Over")
        n.load_asset_outcome_map({"asset-new": "Under"})
        assert n._asset_to_outcome["asset-existing"] == "Over"
        assert n._asset_to_outcome["asset-new"] == "Under"

    def test_update_overwrites_existing(self):
        n = self._make_normalizer()
        n.update_asset_outcome("asset-1", "Up")  # bad Data API value
        n.update_asset_outcome("asset-1", "No")  # corrected from tokens table
        assert n._asset_to_outcome["asset-1"] == "No"

    def test_missing_asset_returns_none_from_get(self):
        n = self._make_normalizer()
        # asset not in map → get returns None (no KeyError)
        assert n._asset_to_outcome.get("unknown-asset") is None

    def test_outcome_map_independent_from_asset_map(self):
        n = self._make_normalizer()
        n.update_asset_map("asset-1", "market-1")
        n.update_asset_outcome("asset-1", "Yes")
        # Both maps populated correctly and independently
        assert n._asset_to_market["asset-1"] == "market-1"
        assert n._asset_to_outcome["asset-1"] == "Yes"

    def test_outcome_map_initially_empty(self):
        n = self._make_normalizer()
        assert len(n._asset_to_outcome) == 0


# ---------------------------------------------------------------------------
# Selection display edge cases
# ---------------------------------------------------------------------------

class TestFmtSelectionEdgeCases:
    def test_empty_string_outcome_returns_empty(self):
        # Empty string is not None, so it passes through as-is
        assert _fmt_selection("", "BUY") == ""

    def test_side_has_no_effect(self):
        assert _fmt_selection("BJP", "UNKNOWN") == "BJP"

    def test_crypto_price_categorical(self):
        assert _fmt_selection("Above $100k", "BUY") == "Above $100k"
        assert _fmt_selection("Below $100k", "SELL") == "Below $100k"
