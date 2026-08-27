"""Unit tests for the ROI-based wallet scoring system."""
import math
import statistics

import pytest

from app.services.wallets.scorer import (
    _FARMING_DAYS_CEILING,
    _FARMING_PRICE_CEILING,
    _MIN_CLOSED_POSITIONS,
    _assign_tier,
    _compute_raw_metrics,
    _is_farming,
    _min_max_normalize,
    compute_log_roi,
)


# ---------------------------------------------------------------------------
# compute_log_roi
# ---------------------------------------------------------------------------

class TestComputeLogROI:
    def test_breakeven_returns_zero(self):
        # exit == entry → log(1 + 0) = 0
        assert compute_log_roi(0.5, 0.5) == 0.0

    def test_win_at_midpoint(self):
        # exit=1.0, entry=0.5 → log(1 + 1.0) = log(2) ≈ 0.693
        result = compute_log_roi(1.0, 0.5)
        assert abs(result - math.log(2.0)) < 1e-9

    def test_total_loss(self):
        # exit=0, entry=0.5 → argument would be 0.0, clamped to 0.01 so a
        # single total loss can't dominate a wallet's weighted log ROI
        result = compute_log_roi(0.0, 0.5)
        assert result is not None
        assert result == pytest.approx(math.log(0.01))  # ≈ -4.6

    def test_partial_gain(self):
        # exit=0.6, entry=0.4 → log(1 + 0.5) = log(1.5)
        result = compute_log_roi(0.6, 0.4)
        assert abs(result - math.log(1.5)) < 1e-9

    def test_zero_entry_returns_none(self):
        assert compute_log_roi(0.5, 0.0) is None

    def test_negative_entry_returns_none(self):
        assert compute_log_roi(0.5, -0.1) is None

    def test_small_gain_positive(self):
        result = compute_log_roi(0.41, 0.40)
        assert result is not None and result > 0

    def test_small_loss_negative(self):
        result = compute_log_roi(0.39, 0.40)
        assert result is not None and result < 0


# ---------------------------------------------------------------------------
# _assign_tier
# ---------------------------------------------------------------------------

class TestAssignTier:
    def test_zero_positions_unranked(self):
        assert _assign_tier(0) == "Unranked"

    def test_nine_positions_unranked(self):
        assert _assign_tier(9) == "Unranked"

    def test_ten_positions_developing(self):
        assert _assign_tier(10) == "Developing"

    def test_twentyfive_positions_developing(self):
        assert _assign_tier(25) == "Developing"

    def test_twentysix_positions_established(self):
        assert _assign_tier(26) == "Established"

    def test_fifty_positions_established(self):
        assert _assign_tier(50) == "Established"

    def test_fiftyone_positions_verified(self):
        assert _assign_tier(51) == "Verified"

    def test_hundred_positions_verified(self):
        assert _assign_tier(100) == "Verified"

    def test_min_closed_positions_constant_matches_tier(self):
        # The cold-start constant must align with tier boundary
        assert _assign_tier(_MIN_CLOSED_POSITIONS) == "Developing"
        assert _assign_tier(_MIN_CLOSED_POSITIONS - 1) == "Unranked"


# ---------------------------------------------------------------------------
# _min_max_normalize
# ---------------------------------------------------------------------------

class TestMinMaxNormalize:
    def test_empty_list(self):
        assert _min_max_normalize([]) == []

    def test_single_value_all_equal_returns_fifty(self):
        assert _min_max_normalize([0.3]) == [50.0]

    def test_all_equal_values_return_fifty(self):
        result = _min_max_normalize([0.5, 0.5, 0.5])
        assert all(v == 50.0 for v in result)

    def test_min_is_zero_max_is_hundred(self):
        result = _min_max_normalize([0.0, 0.5, 1.0])
        assert result[0] == 0.0
        assert result[2] == 100.0

    def test_preserves_order(self):
        values = [0.1, 0.5, 0.9]
        result = _min_max_normalize(values)
        assert result[0] < result[1] < result[2]

    def test_output_length_matches_input(self):
        values = [0.1, 0.3, 0.7, 0.9]
        assert len(_min_max_normalize(values)) == 4

    def test_midpoint_is_fifty(self):
        result = _min_max_normalize([0.0, 0.5, 1.0])
        assert abs(result[1] - 50.0) < 1e-9


# ---------------------------------------------------------------------------
# _compute_raw_metrics
# ---------------------------------------------------------------------------

def _make_positions(n: int, entry: float, exit_: float, shares: float = 100.0):
    """Build synthetic position objects with the fields _compute_raw_metrics needs."""
    class FakePos:
        def __init__(self, e, x, s, w, notional):
            self.log_roi = math.log(max(1e-10, 1.0 + (x - e) / e)) if e > 0 else 0.0
            self.conviction_weight = w
            self.position_fraction = 1.0
            self.notional_usd = notional
            self.exit_price = x
            self.entry_price = e
            self.shares_sold = s
            self.wallet = "test-wallet"

    notional = entry * shares
    return [FakePos(entry, exit_, shares, 1.0 / n, notional) for _ in range(n)]


class TestComputeRawMetrics:
    def test_returns_none_below_threshold(self):
        positions = _make_positions(9, 0.4, 0.8)
        assert _compute_raw_metrics("w", positions) is None

    def test_returns_metrics_at_threshold(self):
        positions = _make_positions(10, 0.4, 0.8)
        result = _compute_raw_metrics("w", positions)
        assert result is not None

    def test_closed_count_correct(self):
        positions = _make_positions(12, 0.4, 0.8)
        m = _compute_raw_metrics("w", positions)
        assert m.closed_count == 12

    def test_all_wins_win_rate_is_one(self):
        positions = _make_positions(10, 0.3, 1.0)  # always win
        m = _compute_raw_metrics("w", positions)
        assert abs(m.win_rate - 1.0) < 1e-9

    def test_all_losses_win_rate_is_zero(self):
        positions = _make_positions(10, 0.3, 0.0)  # always lose
        m = _compute_raw_metrics("w", positions)
        assert m.win_rate == 0.0

    def test_uniform_positions_zero_consistency_stdev(self):
        # Identical log_roi values → stdev=0 → consistency = 1/(1+0) = 1.0
        positions = _make_positions(10, 0.5, 0.8)
        m = _compute_raw_metrics("w", positions)
        assert abs(m.consistency - 1.0) < 1e-9

    def test_realized_pnl_positive_for_wins(self):
        positions = _make_positions(10, 0.4, 1.0)  # profit = 0.6 per share
        m = _compute_raw_metrics("w", positions)
        assert m.realized_pnl > 0

    def test_realized_pnl_negative_for_losses(self):
        positions = _make_positions(10, 0.7, 0.0)  # loss = -0.7 per share
        m = _compute_raw_metrics("w", positions)
        assert m.realized_pnl < 0

    def test_weighted_log_roi_matches_mean_for_equal_weights(self):
        positions = _make_positions(10, 0.4, 0.7)
        m = _compute_raw_metrics("w", positions)
        expected = math.log(1.0 + (0.7 - 0.4) / 0.4)
        assert abs(m.weighted_log_roi - expected) < 1e-9


# ---------------------------------------------------------------------------
# Farming filter (unchanged — FARMER badge still computed from this)
# ---------------------------------------------------------------------------

class TestFarmingFilter:
    def test_both_conditions_is_farming(self):
        assert _is_farming(_FARMING_PRICE_CEILING + 0.01, _FARMING_DAYS_CEILING + 1) is True

    def test_high_price_alone_not_farming(self):
        assert _is_farming(_FARMING_PRICE_CEILING + 0.01, None) is False

    def test_long_horizon_alone_not_farming(self):
        assert _is_farming(0.5, _FARMING_DAYS_CEILING + 1) is False

    def test_at_ceiling_not_farming(self):
        assert _is_farming(_FARMING_PRICE_CEILING, _FARMING_DAYS_CEILING + 1) is False

    def test_below_ceiling_not_farming(self):
        assert _is_farming(0.89, _FARMING_DAYS_CEILING + 1) is False

    def test_at_days_ceiling_not_farming(self):
        assert _is_farming(_FARMING_PRICE_CEILING + 0.01, _FARMING_DAYS_CEILING) is False

    def test_none_price_and_none_days_not_farming(self):
        assert _is_farming(None, None) is False
