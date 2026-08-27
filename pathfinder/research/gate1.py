"""Gate 1 orchestration (PRD §7 Phase 1, architecture §3.3).

Ties together signal loading, markout computation, matched-control
construction, and event-cluster-clustered bootstrap CIs into a single
`run_gate1` entry point producing a `Gate1Result` — the input to the FR-3
report renderer (pathfinder/research/report.py) and the decisions/ gate
verdict entry.

Gate 1 verdict (PRD §7, verbatim): qualified-wallet signals show positive
mean markout net of 1c/side at +6h AND +24h; effect survives removal of
top 1% of signals; qualified group clearly separated from matched control
(non-overlapping bootstrap 90% CIs on mean markout).
"""
from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from statistics import median

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.db.models.event_cluster import EventCluster
from app.db.models.market import Market
from app.db.models.signal import Signal
from pathfinder.config import PathfinderConfig
from pathfinder.research.controls import find_matched_controls
from pathfinder.research.markouts import HORIZONS, SignalMarkout, compute_markouts
from pathfinder.research.stats import BootstrapResult, cluster_bootstrap_ci, remove_top_signals_by_value

ALL_HORIZONS = list(HORIZONS.keys()) + ["resolution"]
GATE1_HORIZONS = ("6h", "24h")


@dataclass(frozen=True)
class _ControlEvent:
    """Markout-computable duck type for a matched control (never persisted
    as a Signal row) -- see pathfinder/research/markouts.MarkoutEvent."""

    id: str
    wallet: str
    market_id: str
    asset_id: str
    signal_time: datetime
    signal_price: Decimal


def _category_bucket(category: str | None, subcategory: str | None) -> str:
    if category == "Politics":
        return "politics"
    if category == "Sports":
        sub = (subcategory or "").lower()
        if any(k in sub for k in ("esports", "counter-strike", "league of legends", "dota", "valorant")):
            return "esports"
        return "sports"
    return "other"


@dataclass(frozen=True)
class HorizonStats:
    horizon: str
    gross: BootstrapResult
    net_base: BootstrapResult  # net of 1c/side
    net_stress: BootstrapResult  # net of 2c/side
    control_gross: BootstrapResult | None
    control_net_base: BootstrapResult | None
    n_with_control: int
    top1pct_removed_net_base_mean: float | None
    gross_median: float
    net_base_median: float


@dataclass(frozen=True)
class Gate1Verdict:
    passed: bool
    reasons: list[str]


@dataclass(frozen=True)
class Gate1Result:
    config_version: str
    n_signals: int
    n_wallets: int
    n_markets: int
    n_clusters: int
    horizon_stats: dict[str, HorizonStats]
    category_counts: dict[str, int]
    top_cluster_concentration: list[tuple[str, int]]
    verdict: Gate1Verdict
    signal_markouts: list[SignalMarkout] = field(repr=False)
    control_markouts: dict[str, SignalMarkout] = field(repr=False)
    cluster_of: dict[str, str] = field(repr=False)


async def _load_cluster_map(session: AsyncSession, market_ids: set[str]) -> dict[str, str]:
    rows = (
        await session.execute(select(EventCluster.market_id, EventCluster.cluster_id).where(EventCluster.market_id.in_(market_ids)))
    ).all()
    cluster_of = {market_id: cluster_id for market_id, cluster_id in rows}
    return {mid: cluster_of.get(mid, f"unclustered:{mid}") for mid in market_ids}


async def run_gate1(
    session: AsyncSession,
    session_factory: async_sessionmaker[AsyncSession],
    config: PathfinderConfig,
    n_boot: int = 2000,
) -> Gate1Result:
    signals = (
        await session.execute(select(Signal).where(Signal.config_version == config.config_version))
    ).scalars().all()
    if not signals:
        raise ValueError(f"no materialized signals for config_version={config.config_version!r}")

    market_ids = {s.market_id for s in signals}
    cluster_of = await _load_cluster_map(session, market_ids)

    signal_markouts = await compute_markouts(session, signals)
    control_match_map = await find_matched_controls(session, session_factory, signals, config)

    control_events = []
    for s in signals:
        m = control_match_map.get(s.id)
        if m is not None:
            control_events.append(
                _ControlEvent(
                    id=s.id, wallet=m.candidate.wallet, market_id=m.candidate.market_id,
                    asset_id=m.candidate.asset_id, signal_time=m.candidate.signal_time,
                    signal_price=m.candidate.signal_price,
                )
            )
    control_markouts_list = await compute_markouts(session, control_events) if control_events else []
    control_markouts = {cm.signal_id: cm for cm in control_markouts_list}

    base_cost = config.costs.base_cost_cents
    stress_cost = config.costs.stress_cost_cents

    horizon_stats: dict[str, HorizonStats] = {}
    for horizon in ALL_HORIZONS:
        gross_vals, gross_clusters = [], []
        net_base_vals, net_base_clusters = [], []
        net_stress_vals, net_stress_clusters = [], []
        ctrl_gross_vals, ctrl_gross_clusters = [], []
        ctrl_net_base_vals, ctrl_net_base_clusters = [], []

        for sm in signal_markouts:
            gross = sm.gross_markout(horizon)
            if gross is None:
                continue
            cid = cluster_of[sm.market_id]
            gross_vals.append(float(gross))
            gross_clusters.append(cid)
            net_base_vals.append(float(sm.net_markout(horizon, base_cost)))
            net_base_clusters.append(cid)
            net_stress_vals.append(float(sm.net_markout(horizon, stress_cost)))
            net_stress_clusters.append(cid)

            cm = control_markouts.get(sm.signal_id)
            if cm is not None:
                cgross = cm.gross_markout(horizon)
                if cgross is not None:
                    ctrl_gross_vals.append(float(cgross))
                    ctrl_gross_clusters.append(cid)
                    ctrl_net_base_vals.append(float(cm.net_markout(horizon, base_cost)))
                    ctrl_net_base_clusters.append(cid)

        if not gross_vals:
            continue

        top1_removed = remove_top_signals_by_value(net_base_vals, pct=0.01)

        horizon_stats[horizon] = HorizonStats(
            horizon=horizon,
            gross=cluster_bootstrap_ci(gross_vals, gross_clusters, n_boot=n_boot),
            net_base=cluster_bootstrap_ci(net_base_vals, net_base_clusters, n_boot=n_boot),
            net_stress=cluster_bootstrap_ci(net_stress_vals, net_stress_clusters, n_boot=n_boot),
            control_gross=cluster_bootstrap_ci(ctrl_gross_vals, ctrl_gross_clusters, n_boot=n_boot) if ctrl_gross_vals else None,
            control_net_base=cluster_bootstrap_ci(ctrl_net_base_vals, ctrl_net_base_clusters, n_boot=n_boot) if ctrl_net_base_vals else None,
            n_with_control=len(ctrl_gross_vals),
            top1pct_removed_net_base_mean=(sum(top1_removed) / len(top1_removed)) if top1_removed else None,
            gross_median=median(gross_vals),
            net_base_median=median(net_base_vals),
        )

    verdict = _evaluate_gate1(horizon_stats)

    market_rows = (await session.execute(select(Market.market_id, Market.category, Market.subcategory).where(Market.market_id.in_(market_ids)))).all()
    market_meta = {mid: (cat, sub) for mid, cat, sub in market_rows}
    category_counts: dict[str, int] = {}
    for s in signals:
        cat, sub = market_meta.get(s.market_id, (None, None))
        bucket = _category_bucket(cat, sub)
        category_counts[bucket] = category_counts.get(bucket, 0) + 1

    cluster_counts = Counter(cluster_of[s.market_id] for s in signals)
    top_cluster_concentration = cluster_counts.most_common(10)

    return Gate1Result(
        config_version=config.config_version,
        n_signals=len(signals),
        n_wallets=len({s.wallet for s in signals}),
        n_markets=len(market_ids),
        n_clusters=len(set(cluster_of.values())),
        horizon_stats=horizon_stats,
        category_counts=category_counts,
        top_cluster_concentration=top_cluster_concentration,
        verdict=verdict,
        signal_markouts=signal_markouts,
        control_markouts=control_markouts,
        cluster_of=cluster_of,
    )


def _evaluate_gate1(horizon_stats: dict[str, HorizonStats]) -> Gate1Verdict:
    reasons: list[str] = []

    for horizon in GATE1_HORIZONS:
        hs = horizon_stats.get(horizon)
        if hs is None:
            reasons.append(f"{horizon}: no data")
            continue
        if hs.net_base.mean <= 0:
            reasons.append(f"{horizon}: mean net markout (base cost) is {hs.net_base.mean:.4f}, not positive")
        if hs.top1pct_removed_net_base_mean is not None and hs.top1pct_removed_net_base_mean <= 0:
            reasons.append(f"{horizon}: mean net markout after top-1% removal is {hs.top1pct_removed_net_base_mean:.4f}, not positive")
        if hs.control_net_base is None:
            reasons.append(f"{horizon}: no control comparison available (no matched controls)")
        else:
            overlap = not (hs.net_base.ci_low > hs.control_net_base.ci_high or hs.control_net_base.ci_low > hs.net_base.ci_high)
            if overlap:
                reasons.append(
                    f"{horizon}: qualified 90% CI [{hs.net_base.ci_low:.4f}, {hs.net_base.ci_high:.4f}] "
                    f"overlaps control 90% CI [{hs.control_net_base.ci_low:.4f}, {hs.control_net_base.ci_high:.4f}]"
                )

    return Gate1Verdict(passed=len(reasons) == 0, reasons=reasons)
