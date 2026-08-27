"""Pydantic-validated loader for config/pathfinder.yaml.

One versioned YAML holds every Pathfinder strategy parameter (architecture
§5). Unknown keys are errors — a typo or a stray key must fail loudly, not
be silently ignored.
"""
from __future__ import annotations

from functools import lru_cache
from pathlib import Path

import yaml
from pydantic import BaseModel, ConfigDict

_DEFAULT_CONFIG_PATH = Path(__file__).resolve().parent.parent / "config" / "pathfinder.yaml"


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class ScoringConfig(_StrictModel):
    formation_window_days: int
    min_closed_positions: int
    min_mean_roi: float
    skill_vs_implied_required: bool
    concentration_cap: float
    sybil_exclusion_enabled: bool
    excluded_closed_at_sources: list[str]


class SignalConfig(_StrictModel):
    signal_min_notional_usd: float
    accumulation_window_minutes: int
    price_floor: float
    price_ceiling: float
    volume_floor_usd: float
    volume_window_hours: int
    min_hours_to_resolution: int
    detection_lag_seconds: int


class EntryConfig(_StrictModel):
    entry_delay_minutes: int
    slippage_allowance_cents: int


class SizingConfig(_StrictModel):
    per_signal_size_pct: float
    max_concurrent_positions: int
    max_per_market_pct: float
    max_per_event_cluster_pct: float
    max_gross_exposure_pct: float


class ExitsConfig(_StrictModel):
    follow_exit_reduction_pct: float
    take_profit_multiple: float
    take_profit_sell_pct: float
    trailing_stop_pct: float
    synthetic_stop_multiple: float
    stop_escalation_wait_minutes: int
    cooldown_hours: int


class CostsConfig(_StrictModel):
    base_cost_cents: float
    stress_cost_cents: float
    gas_usd: float


class RiskConfig(_StrictModel):
    daily_loss_pause_pct: float
    soft_pause_dd_pct: float
    hard_stop_dd_pct: float
    detection_lag_pause_multiple: float
    stale_feed_minutes: int
    consecutive_rejections_pause: int


class DaemonConfig(_StrictModel):
    qualified_set_refresh_hours: int


class AlertsConfig(_StrictModel):
    alert_only_mode: bool


class BooklogConfig(_StrictModel):
    snapshot_interval_seconds: int
    book_depth_levels: int
    volume_floor_usd: float
    watchlist_event_titles: list[str] = []


class GridConfig(_StrictModel):
    min_mean_roi: list[float]
    entry_delay_minutes: list[int]
    slippage_allowance_cents: list[int]
    formation_window_days: list[int]


class PathfinderConfig(_StrictModel):
    config_version: str
    scoring: ScoringConfig
    signal: SignalConfig
    entry: EntryConfig
    sizing: SizingConfig
    exits: ExitsConfig
    costs: CostsConfig
    risk: RiskConfig
    daemon: DaemonConfig
    alerts: AlertsConfig
    booklog: BooklogConfig
    grid: GridConfig


def load_config(path: Path | str | None = None) -> PathfinderConfig:
    """Load and validate config/pathfinder.yaml (or an explicit override path)."""
    config_path = Path(path) if path is not None else _DEFAULT_CONFIG_PATH
    with open(config_path, "r", encoding="utf-8") as f:
        raw = yaml.safe_load(f)
    return PathfinderConfig.model_validate(raw)


@lru_cache
def get_config() -> PathfinderConfig:
    return load_config()
