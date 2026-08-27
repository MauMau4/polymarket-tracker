"""Cluster bootstrap CI and outlier-robustness helpers (PRD §7, Gate 1).

No numpy/scipy dependency — this repo has neither, and the percentile /
resampling math needed here is small enough to keep in the stdlib
(`random`, `statistics`), consistent with the project's existing minimal-
dependency style.

Cluster bootstrap (not plain i.i.d. resampling): Gate 1's 274 signals
concentrate 79.6% into 7 markets sharing one event cluster (decisions/
2026-07-17.md) — resampling individual signals would treat those as 274
independent observations and badly understate uncertainty. Resampling
CLUSTERS (architecture §3.3's matched-control unit, `event_clusters
.cluster_id`, falling back to the market itself when unclustered) with
replacement, then pooling every signal belonging to each drawn cluster,
is the standard cluster-bootstrap correction.
"""
from __future__ import annotations

import math
import random
from dataclasses import dataclass
from statistics import mean

DEFAULT_BOOTSTRAP_SEED = 42
DEFAULT_N_BOOT = 2000


@dataclass(frozen=True)
class BootstrapResult:
    mean: float
    ci_low: float
    ci_high: float
    n_observations: int
    n_clusters: int
    n_boot: int
    ci_level: float


def _percentile(sorted_values: list[float], pct: float) -> float:
    """Linear-interpolation percentile (matches numpy's default 'linear' method)."""
    if len(sorted_values) == 1:
        return sorted_values[0]
    idx = pct / 100 * (len(sorted_values) - 1)
    lo = int(idx)
    hi = min(lo + 1, len(sorted_values) - 1)
    frac = idx - lo
    return sorted_values[lo] + (sorted_values[hi] - sorted_values[lo]) * frac


def cluster_bootstrap_ci(
    values: list[float],
    cluster_ids: list[str],
    n_boot: int = DEFAULT_N_BOOT,
    ci_level: float = 0.90,
    seed: int = DEFAULT_BOOTSTRAP_SEED,
) -> BootstrapResult:
    """values[i] belongs to cluster_ids[i]. Resamples clusters (not
    individual values) with replacement, `n_boot` times, pooling every
    value in each drawn cluster; returns the observed mean plus the
    `ci_level` percentile interval of the bootstrap distribution."""
    if len(values) != len(cluster_ids):
        raise ValueError("values and cluster_ids must be the same length")
    if not values:
        raise ValueError("no observations to bootstrap")

    by_cluster: dict[str, list[float]] = {}
    for v, c in zip(values, cluster_ids):
        by_cluster.setdefault(c, []).append(v)
    unique_clusters = list(by_cluster.keys())

    rng = random.Random(seed)
    boot_means = []
    for _ in range(n_boot):
        drawn = rng.choices(unique_clusters, k=len(unique_clusters))
        pooled = [v for c in drawn for v in by_cluster[c]]
        boot_means.append(mean(pooled))
    boot_means.sort()

    alpha = (1 - ci_level) / 2
    return BootstrapResult(
        mean=mean(values),
        ci_low=_percentile(boot_means, alpha * 100),
        ci_high=_percentile(boot_means, (1 - alpha) * 100),
        n_observations=len(values),
        n_clusters=len(unique_clusters),
        n_boot=n_boot,
        ci_level=ci_level,
    )


def remove_top_signals_by_value(values: list[float], pct: float = 0.01) -> list[float]:
    """Drops the highest `pct` fraction of values (PRD §7's "top 1% of
    signals" robustness check). Rounds the removal count UP (`ceil`), never
    down to zero — for N=274, 1% -> 3 removed, not 2 (a check that removes
    nothing for small N would be vacuous)."""
    if not values:
        return []
    n_remove = max(1, math.ceil(len(values) * pct))
    return sorted(values)[: max(0, len(values) - n_remove)]
