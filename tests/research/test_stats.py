"""Cluster bootstrap CI and top-1%-removal robustness helpers."""
import pytest

from pathfinder.research.stats import cluster_bootstrap_ci, remove_top_signals_by_value


def test_bootstrap_mean_matches_plain_mean():
    values = [1.0, 2.0, 3.0, 4.0]
    clusters = ["a", "b", "c", "d"]
    result = cluster_bootstrap_ci(values, clusters, n_boot=500)
    assert result.mean == pytest.approx(2.5)
    assert result.n_observations == 4
    assert result.n_clusters == 4


def test_single_cluster_collapses_ci_to_a_point():
    # Every value shares one cluster -- every bootstrap draw re-pools the
    # exact same cluster, so the bootstrap mean is identical every time.
    values = [1.0, 5.0, 100.0, -50.0]
    clusters = ["only"] * 4
    result = cluster_bootstrap_ci(values, clusters, n_boot=200)
    assert result.n_clusters == 1
    assert result.ci_low == pytest.approx(result.mean)
    assert result.ci_high == pytest.approx(result.mean)


def test_more_clusters_narrower_ci_than_one_dominant_cluster():
    # Same 8 observations, same total, but grouped into 8 independent
    # clusters vs. one dominant cluster holding 6 of them -- the dominant-
    # cluster case must show wider uncertainty (fewer independent draws).
    values = [1.0, 2.0, 3.0, 4.0, 5.0, 6.0, 100.0, -90.0]
    many_clusters = [f"c{i}" for i in range(8)]
    one_dominant = ["dom"] * 6 + ["c6", "c7"]

    wide = cluster_bootstrap_ci(values, one_dominant, n_boot=2000)
    narrow = cluster_bootstrap_ci(values, many_clusters, n_boot=2000)

    assert (wide.ci_high - wide.ci_low) > (narrow.ci_high - narrow.ci_low)


def test_deterministic_with_same_seed():
    values = [1.0, 2.0, 3.0, 4.0, 5.0]
    clusters = ["a", "b", "c", "d", "e"]
    r1 = cluster_bootstrap_ci(values, clusters, n_boot=300, seed=7)
    r2 = cluster_bootstrap_ci(values, clusters, n_boot=300, seed=7)
    assert r1.ci_low == r2.ci_low
    assert r1.ci_high == r2.ci_high


def test_different_seed_can_change_ci():
    values = [1.0, 2.0, 3.0, 4.0, 5.0, 6.0, 7.0]
    clusters = ["a", "b", "c", "d", "e", "f", "g"]
    r1 = cluster_bootstrap_ci(values, clusters, n_boot=50, seed=1)
    r2 = cluster_bootstrap_ci(values, clusters, n_boot=50, seed=2)
    assert (r1.ci_low, r1.ci_high) != (r2.ci_low, r2.ci_high)


def test_empty_values_raises():
    with pytest.raises(ValueError):
        cluster_bootstrap_ci([], [])


def test_mismatched_lengths_raises():
    with pytest.raises(ValueError):
        cluster_bootstrap_ci([1.0, 2.0], ["a"])


def test_remove_top_1pct_of_274_removes_3():
    values = list(range(274))  # 0..273
    remaining = remove_top_signals_by_value([float(v) for v in values], pct=0.01)
    assert len(remaining) == 271
    assert max(remaining) == 270.0  # top 3 (273, 272, 271) removed


def test_remove_top_pct_never_removes_zero_for_small_n():
    values = [1.0, 2.0, 3.0, 4.0, 5.0]
    remaining = remove_top_signals_by_value(values, pct=0.01)
    assert len(remaining) == 4
    assert max(remaining) == 4.0


def test_remove_top_pct_empty_input():
    assert remove_top_signals_by_value([]) == []
