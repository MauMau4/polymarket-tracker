"""Event-study report renderer (PRD §8, architecture §3.3).

Fixed format per PRD §8: "mean/median markout per horizon (net & gross),
CI, N, control comparison, persistence correlation, per-category breakdown
(sports/esports/politics/other), top-signal concentration."

Gate 2 (persistence correlation) is deferred this run (decisions/
2026-07-17.md — 0 of 88-128 qualified wallets, at every declared grid
cell, have sufficient WCP history span for even one consecutive-window
pair) — the report states this plainly rather than reporting a hollow or
fabricated correlation, per pathfinder/CLAUDE.md rule 7.
"""
from __future__ import annotations

from datetime import datetime, timezone

from pathfinder.config import PathfinderConfig
from pathfinder.research.gate1 import ALL_HORIZONS, GATE1_HORIZONS, Gate1Result, HorizonStats


def _fmt_pct(x: float, decimals: int = 2) -> str:
    return f"{x * 100:.{decimals}f}%"


def _horizon_table_row(hs: HorizonStats) -> str:
    ctrl = (
        f"mean={_fmt_pct(hs.control_net_base.mean)} CI=[{_fmt_pct(hs.control_net_base.ci_low)}, {_fmt_pct(hs.control_net_base.ci_high)}] (n={hs.n_with_control})"
        if hs.control_net_base is not None
        else "no control"
    )
    return (
        f"| {hs.horizon} | {_fmt_pct(hs.gross.mean)} | {_fmt_pct(hs.gross_median)} | "
        f"{_fmt_pct(hs.net_base.mean)} | {_fmt_pct(hs.net_base_median)} | "
        f"[{_fmt_pct(hs.net_base.ci_low)}, {_fmt_pct(hs.net_base.ci_high)}] | "
        f"{_fmt_pct(hs.net_stress.mean)} | "
        f"{hs.net_base.n_observations} ({hs.net_base.n_clusters} clusters) | "
        f"{ctrl} |"
    )


def render_event_study_report(result: Gate1Result, config: PathfinderConfig, generated_at: datetime | None = None) -> str:
    generated_at = generated_at or datetime.now(tz=timezone.utc)
    lines: list[str] = []
    a = lines.append

    a(f"# Event Study Report — Pathfinder M2, Gate 1 & 2")
    a("")
    a(f"**config_version:** `{config.config_version}` | **generated:** {generated_at.isoformat()}")
    a(f"**Cells:** `min_mean_roi={config.scoring.min_mean_roi}`, `formation_window_days={config.scoring.formation_window_days}` "
      f"(default cell — see decisions/2026-07-17.md for the full declared-grid range)")
    a("")
    a("## Sample")
    a("")
    a(f"- Signals: **{result.n_signals}**")
    a(f"- Distinct wallets: **{result.n_wallets}**")
    a(f"- Distinct markets: **{result.n_markets}**")
    a(f"- Distinct effective clusters (event_cluster or singleton market): **{result.n_clusters}**")
    a("")
    a("**Known limitations, established this session (decisions/2026-07-17.md) — read before interpreting the numbers below:**")
    a("")
    a("1. No UMA-active-dispute filter is applied (PRD §5.2) — Polymarket dispute status isn't ingested anywhere in this schema. "
      "Every signal here passes every §5.2 filter we can currently check, not a dispute-verified one.")
    a("2. Wallet qualification (and therefore this entire signal set) has an unquantified undercount bias: discovery/subscription "
      "coverage gaps mean wallets active in uncovered market genres (spot-checked: weather markets, granular sports props, some "
      "politics markets) are severely undercounted, independent of the already-documented sub-$20 fill floor. Direction is "
      "understood (undercounts only, never inflates) but the *rate* across the broader wallet universe is unverified — only 4 "
      "wallets were spot-checked against live Data API.")
    a("3. Signal concentration is severe: one event cluster (World Cup Winner) accounts for "
      f"{result.top_cluster_concentration[0][1]}/{result.n_signals} ({_fmt_pct(result.top_cluster_concentration[0][1] / result.n_signals)}) "
      "of all signals. All CIs below are cluster-bootstrapped (resampling event clusters, not signals) specifically because of this.")
    a("4. The pre-07-11 vs post-07-11 print-volume regime change (originally suspected to be phantom `price_change` trade "
      "contamination) was investigated and found to be a real, one-time WS-subscription contraction following a resolver fix — "
      "not a data-quality defect. No era exclusion or phantom filtering was applied to this signal set as a result "
      "(decisions/2026-07-17.md).")
    a("")

    a("## Category breakdown")
    a("")
    a("| category | signals |")
    a("|---|---|")
    for cat in ("sports", "esports", "politics", "other"):
        a(f"| {cat} | {result.category_counts.get(cat, 0)} |")
    a("")

    a("## Top-signal concentration (by effective cluster)")
    a("")
    a("| cluster | signals | % of total |")
    a("|---|---|---|")
    for cluster_id, count in result.top_cluster_concentration:
        a(f"| `{cluster_id}` | {count} | {_fmt_pct(count / result.n_signals)} |")
    a("")

    a("## Markouts per horizon")
    a("")
    a("Net figures use the base cost tier (1c/side, PRD §5.6); the stress tier (2c/side) column is gross-of-control, "
      "reported separately per PRD §7.2's \"Gate 1 must pass at base; Gate 3 reported at both\" convention. "
      "CIs are 90%, event-cluster bootstrap (2000 draws, seed=42).")
    a("")
    a("| horizon | gross mean | gross median | net mean (1c) | net median (1c) | net 90% CI (1c) | net mean (2c) | N (clusters) | matched control net mean (1c) |")
    a("|---|---|---|---|---|---|---|---|---|")
    for horizon in ALL_HORIZONS:
        hs = result.horizon_stats.get(horizon)
        if hs is not None:
            a(_horizon_table_row(hs))
    a("")

    a("## Top-1% removal robustness (net markout, base cost)")
    a("")
    a("| horizon | mean net markout after removing top 1% of signals |")
    a("|---|---|")
    for horizon in ALL_HORIZONS:
        hs = result.horizon_stats.get(horizon)
        if hs is not None and hs.top1pct_removed_net_base_mean is not None:
            a(f"| {horizon} | {_fmt_pct(hs.top1pct_removed_net_base_mean)} |")
    a("")

    a("## Persistence analysis (Gate 2) — DEFERRED")
    a("")
    a("Not run this session. Feasibility check (decisions/2026-07-17.md): **0 of 88–128 qualified wallets, at every one of "
      "the 9 declared (min_mean_roi, formation_window_days) cells, have sufficient `wallet_closed_positions` history span** "
      "(needs >= 2x formation_window_days) to contribute even one consecutive formation-window pair to the Spearman "
      "persistence test. Root cause: trade/WCP history only extends to 2026-04-25 (~84 days as of this session), which is "
      "less than the ~120-360 days every declared cell needs. This is not a thin sample — it's exactly zero. Revisit when "
      "history suffices (earliest theoretical dates, upper bounds not promises: fw=60d ~2026-08-23, fw=90d ~2026-10-22, "
      "fw=180d ~2027-04-20).")
    a("")

    a("## Gate 1 verdict")
    a("")
    a(f"**{'PASS' if result.verdict.passed else 'FAIL'}**")
    a("")
    if result.verdict.reasons:
        a("Reasons:")
        a("")
        for r in result.verdict.reasons:
            a(f"- {r}")
    else:
        a("All Gate 1 criteria met at +6h and +24h: positive mean net markout (base cost), survives top-1% signal removal, "
          "qualified group's 90% CI does not overlap the matched control's 90% CI.")
    a("")

    a("## Gate 2 verdict")
    a("")
    a("**DEFERRED** — see Persistence analysis section above. Per PRD §7, either gate failing stops the project; a deferral "
      "is neither pass nor fail and does not by itself stop the project, but Gate 2 cannot be marked PASS or FAIL with zero "
      "eligible wallets, and no result should be reported as if it were one.")
    a("")

    return "\n".join(lines)
