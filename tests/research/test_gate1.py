"""Pure-logic tests for Gate 1 verdict evaluation and category bucketing —
no DB needed (integration coverage of run_gate1 itself lives in the smoke-
tested production run, per pathfinder/CLAUDE.md rule 7: this is real
production data, not a synthetic ground-truth fixture, so it's reported in
the study report rather than asserted against in a unit test)."""
from pathfinder.research.gate1 import GATE1_HORIZONS, HorizonStats, _category_bucket, _evaluate_gate1
from pathfinder.research.stats import BootstrapResult


def _bs(mean: float, ci_low: float, ci_high: float, n_clusters: int = 5) -> BootstrapResult:
    return BootstrapResult(mean=mean, ci_low=ci_low, ci_high=ci_high, n_observations=20, n_clusters=n_clusters, n_boot=100, ci_level=0.90)


def _hs(horizon: str, net_mean: float, net_ci: tuple[float, float], control_ci: tuple[float, float] | None, top1_mean: float | None) -> HorizonStats:
    return HorizonStats(
        horizon=horizon,
        gross=_bs(net_mean + 0.01, net_ci[0] + 0.01, net_ci[1] + 0.01),
        net_base=_bs(net_mean, net_ci[0], net_ci[1]),
        net_stress=_bs(net_mean - 0.01, net_ci[0] - 0.01, net_ci[1] - 0.01),
        control_gross=None,
        control_net_base=_bs(*[(control_ci[0] + control_ci[1]) / 2, control_ci[0], control_ci[1]]) if control_ci else None,
        n_with_control=20 if control_ci else 0,
        top1pct_removed_net_base_mean=top1_mean,
        gross_median=net_mean + 0.01,
        net_base_median=net_mean,
    )


def test_category_bucket_politics():
    assert _category_bucket("Politics", None) == "politics"


def test_category_bucket_sports_soccer():
    assert _category_bucket("Sports", "Soccer") == "sports"


def test_category_bucket_sports_esports():
    assert _category_bucket("Sports", "Counter-Strike") == "esports"


def test_category_bucket_other():
    assert _category_bucket("World", None) == "other"
    assert _category_bucket(None, None) == "other"
    assert _category_bucket("Economics", None) == "other"


def test_gate1_passes_when_all_conditions_met():
    horizon_stats = {
        h: _hs(h, net_mean=0.02, net_ci=(0.01, 0.03), control_ci=(-0.01, 0.0), top1_mean=0.015)
        for h in GATE1_HORIZONS
    }
    verdict = _evaluate_gate1(horizon_stats)
    assert verdict.passed is True
    assert verdict.reasons == []


def test_gate1_fails_on_negative_net_markout():
    horizon_stats = {
        h: _hs(h, net_mean=-0.005, net_ci=(-0.01, 0.0), control_ci=(-0.02, -0.01), top1_mean=-0.004)
        for h in GATE1_HORIZONS
    }
    verdict = _evaluate_gate1(horizon_stats)
    assert verdict.passed is False
    assert any("not positive" in r for r in verdict.reasons)


def test_gate1_fails_on_top1pct_removal_flip():
    horizon_stats = {
        h: _hs(h, net_mean=0.02, net_ci=(0.01, 0.03), control_ci=(-0.01, 0.0), top1_mean=-0.001)
        for h in GATE1_HORIZONS
    }
    verdict = _evaluate_gate1(horizon_stats)
    assert verdict.passed is False
    assert any("top-1%" in r for r in verdict.reasons)


def test_gate1_fails_on_overlapping_cis():
    horizon_stats = {
        h: _hs(h, net_mean=0.02, net_ci=(0.01, 0.03), control_ci=(0.015, 0.025), top1_mean=0.015)
        for h in GATE1_HORIZONS
    }
    verdict = _evaluate_gate1(horizon_stats)
    assert verdict.passed is False
    assert any("overlaps control" in r for r in verdict.reasons)


def test_gate1_fails_on_missing_control():
    horizon_stats = {
        h: _hs(h, net_mean=0.02, net_ci=(0.01, 0.03), control_ci=None, top1_mean=0.015)
        for h in GATE1_HORIZONS
    }
    verdict = _evaluate_gate1(horizon_stats)
    assert verdict.passed is False
    assert any("no control comparison" in r for r in verdict.reasons)


def test_gate1_fails_on_missing_horizon_data():
    verdict = _evaluate_gate1({})
    assert verdict.passed is False
    assert all("no data" in r for r in verdict.reasons)
    assert len(verdict.reasons) == len(GATE1_HORIZONS)


def test_gate1_non_overlapping_ci_below_also_passes_the_separation_check():
    # Control CI strictly ABOVE the qualified CI is still "non-overlapping"
    # (separation doesn't require the qualified group to be on top) --
    # exercised here for completeness even though the passing scenario
    # above already covers qualified-above-control.
    horizon_stats = {
        h: _hs(h, net_mean=0.05, net_ci=(0.04, 0.06), control_ci=(0.07, 0.08), top1_mean=0.045)
        for h in GATE1_HORIZONS
    }
    verdict = _evaluate_gate1(horizon_stats)
    # Still fails Gate 1 on separation direction? No -- non-overlap is symmetric;
    # this should PASS since [0.04,0.06] and [0.07,0.08] don't overlap.
    assert verdict.passed is True
