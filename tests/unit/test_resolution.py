"""Unit tests for market resolution processing and Welford's algorithm."""
import math
import pytest

from app.services.markets.resolution import _welford_update, _welford_stdev


class TestWelfordUpdate:
    def test_single_sample(self):
        n, mean, m2 = _welford_update(0, 0.0, 0.0, 0.5)
        assert n == 1
        assert abs(mean - 0.5) < 1e-9
        assert m2 == 0.0

    def test_two_identical_samples(self):
        n, mean, m2 = _welford_update(0, 0.0, 0.0, 0.3)
        n, mean, m2 = _welford_update(n, mean, m2, 0.3)
        assert n == 2
        assert abs(mean - 0.3) < 1e-9
        assert abs(m2) < 1e-9  # identical values → zero variance

    def test_running_mean_matches_simple_avg(self):
        values = [0.1, 0.3, 0.5, 0.7, 0.9]
        n, mean, m2 = 0, 0.0, 0.0
        for v in values:
            n, mean, m2 = _welford_update(n, mean, m2, v)
        expected_mean = sum(values) / len(values)
        assert abs(mean - expected_mean) < 1e-9

    def test_welford_stdev_matches_statistics(self):
        import statistics
        values = [0.1, 0.25, 0.4, 0.6, 0.8]
        n, mean, m2 = 0, 0.0, 0.0
        for v in values:
            n, mean, m2 = _welford_update(n, mean, m2, v)
        welford_std = _welford_stdev(n, m2)
        stats_std = statistics.stdev(values)
        assert abs(welford_std - stats_std) < 1e-9

    def test_stdev_zero_for_identical_values(self):
        values = [0.5, 0.5, 0.5, 0.5]
        n, mean, m2 = 0, 0.0, 0.0
        for v in values:
            n, mean, m2 = _welford_update(n, mean, m2, v)
        assert _welford_stdev(n, m2) == 0.0

    def test_stdev_single_sample_is_zero(self):
        n, mean, m2 = _welford_update(0, 0.0, 0.0, 0.75)
        assert _welford_stdev(n, m2) == 0.0

    def test_incremental_equals_batch(self):
        """Incremental Welford produces same stdev as batch calculation."""
        import statistics
        values = [0.2, 0.4, 0.3, 0.7, 0.1, 0.9, 0.5]
        n, mean, m2 = 0, 0.0, 0.0
        for v in values:
            n, mean, m2 = _welford_update(n, mean, m2, v)
        assert abs(_welford_stdev(n, m2) - statistics.stdev(values)) < 1e-9

    def test_mean_correct_after_many_updates(self):
        import statistics
        values = [i * 0.01 for i in range(1, 51)]
        n, mean, m2 = 0, 0.0, 0.0
        for v in values:
            n, mean, m2 = _welford_update(n, mean, m2, v)
        assert abs(mean - statistics.mean(values)) < 1e-9


class TestWelfordStdev:
    def test_n_less_than_2_returns_zero(self):
        assert _welford_stdev(0, 10.0) == 0.0
        assert _welford_stdev(1, 10.0) == 0.0

    def test_n_equals_2(self):
        # Two samples: 0.0 and 1.0 → stdev = sqrt((0.5)^2 + (0.5)^2) = sqrt(0.5)
        n, mean, m2 = _welford_update(0, 0.0, 0.0, 0.0)
        n, mean, m2 = _welford_update(n, mean, m2, 1.0)
        expected = math.sqrt(m2 / (n - 1))
        assert abs(_welford_stdev(n, m2) - expected) < 1e-9

    def test_positive_m2(self):
        n, mean, m2 = 10, 0.5, 1.0
        result = _welford_stdev(n, m2)
        assert result > 0
        assert abs(result - math.sqrt(1.0 / 9)) < 1e-9
