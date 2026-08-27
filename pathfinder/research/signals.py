"""Signal enumeration (architecture §3.3, PRD §5.2, FR-3).

Enumerates historical signals: a qualified wallet accumulating net notional
in one direction of one market within a rolling window, filtered per PRD
§5.2, with `detection_lag` added to every signal timestamp.

Reads `trades_full` (never `trades` alone — the live table purges; see
pathfinder/CLAUDE.md known-constraints) and calls `pathfinder.scoring.engine
.score_wallet` for qualification — the one scoring code path (architecture
design principle #2), never reimplemented here.

Judgment calls made where the PRD/architecture text underspecifies the
mechanism (same practice as pathfinder/scoring/engine.py — logged in
decisions/2026-07-17.md):

- **(wallet, market, side) -> (wallet, asset_id).** Polymarket has no naked
  shorting: a market's two outcome tokens (e.g. Yes/No) are its two
  "directions". Net BUY-dominant accumulation on one asset_id within the
  window *is* "accumulates net >= X in one direction in one market" —
  partitioning the rolling sum by (wallet, asset_id) captures this exactly.
  Net SELL-dominant activity is a position reduction (PRD §5.5 follow-exit
  territory), not a new accumulation signal, so only upward (BUY-dominant)
  crossings are detected.
- **Rising-edge firing.** A signal fires the first time the trailing-window
  net notional crosses from below `signal_min_notional_usd` to at-or-above
  it. It does not re-fire on every subsequent trade while the window stays
  elevated — only after the net drops back below threshold and re-crosses.
  Without this, one accumulation episode would emit one signal per trade
  for as long as the position stayed large, which is not what "a signal
  fires when a wallet accumulates X" describes.
- **signal_price / accumulated_notional** are the triggering trade's own
  print price and the window's net notional at the moment of crossing
  (may overshoot `signal_min_notional_usd` if the triggering trade itself
  is large).
- **Qualification `as_of` = `detected_time`** (`signal_time +
  detection_lag`), not `signal_time` — that's the earliest moment the
  system could plausibly know about and act on the signal, consistent with
  entry timing elsewhere in the pipeline (architecture §3.3/§5.3).
- **`market_trailing_volume`** includes unattributed-wallet trades — real
  dollar volume traded is what makes a market liquid, independent of
  whether we could attribute the counterparty wallet.
- **`markets.end_date`** is read at its current stored value, not
  reconstructed point-in-time (no history table exists for it) — same
  class of accepted limitation as the Sybil-flag current-state proxy in
  pathfinder/scoring/engine.py.
- **UMA dispute filter is NOT implemented.** Polymarket dispute status
  isn't ingested anywhere in this schema (docs/m1-schema-audit.md Q4,
  decisions/2026-07-15.md, pathfinder/CLAUDE.md known-constraints) — a
  real, documented gap, not silently papered over. Every signal in this
  module's output should be read as "passes every §5.2 filter we can
  currently check."
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal

from sqlalchemy import text
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.db.models.signal import Signal
from pathfinder.config import PathfinderConfig
from pathfinder.scoring.engine import score_wallet

_CROSSING_SQL = text(
    """
    WITH trade_base AS (
        SELECT wallet, asset_id, market_id, ts, price, notional_usd, side, outcome,
            CASE WHEN side = 'BUY' THEN notional_usd ELSE -notional_usd END AS signed_notional
        FROM trades_full
        WHERE notional_usd IS NOT NULL AND side IN ('BUY', 'SELL') AND price IS NOT NULL
    ),
    with_mvol AS (
        SELECT *,
            SUM(notional_usd) OVER (
                PARTITION BY market_id ORDER BY ts
                RANGE BETWEEN make_interval(hours => :volume_window_hours) PRECEDING AND CURRENT ROW
            ) AS market_trailing_volume
        FROM trade_base
    ),
    rolling AS (
        SELECT *,
            SUM(signed_notional) OVER (
                PARTITION BY wallet, asset_id ORDER BY ts
                RANGE BETWEEN make_interval(mins => :accumulation_window_minutes) PRECEDING AND CURRENT ROW
            ) AS net_notional
        FROM with_mvol
        WHERE wallet IS NOT NULL
    ),
    crossing AS (
        SELECT *,
            LAG(net_notional) OVER (PARTITION BY wallet, asset_id ORDER BY ts) AS prev_net_notional
        FROM rolling
    ),
    candidates AS (
        SELECT wallet, asset_id, market_id, outcome AS side, ts AS signal_time,
               price AS signal_price, net_notional AS accumulated_notional, market_trailing_volume
        FROM crossing
        WHERE net_notional >= :signal_min_notional_usd
          AND (prev_net_notional IS NULL OR prev_net_notional < :signal_min_notional_usd)
    )
    SELECT
        c.wallet, c.asset_id, c.market_id, c.side, c.signal_time, c.signal_price,
        c.accumulated_notional, c.market_trailing_volume, m.end_date,
        (c.signal_price BETWEEN :price_floor AND :price_ceiling) AS passes_price,
        (m.end_date IS NOT NULL
            AND m.end_date - c.signal_time >= make_interval(hours => :min_hours_to_resolution)) AS passes_time,
        (c.market_trailing_volume >= :volume_floor_usd) AS passes_volume
    FROM candidates c
    JOIN markets m ON m.market_id = c.market_id
    ORDER BY c.wallet, c.asset_id, c.signal_time
    """
)


@dataclass(frozen=True)
class EnumerationFunnel:
    raw_crossings: int
    after_price_band: int
    after_time_to_resolution: int
    after_volume_floor: int
    after_qualification: int
    distinct_wallets_final: int
    distinct_markets_final: int


@dataclass(frozen=True)
class SignalRecord:
    wallet: str
    market_id: str
    asset_id: str
    side: str | None
    signal_time: datetime
    detected_time: datetime
    signal_price: Decimal
    accumulated_notional: Decimal
    config_version: str
    phase: str = "backtest"


@dataclass(frozen=True)
class SignalEnumerationResult:
    funnel: EnumerationFunnel
    signals: list[SignalRecord]


async def _market_filtered_candidates(session: AsyncSession, config: PathfinderConfig):
    """Run the crossing-detection + price/time/volume-filter SQL pass.

    Returns every crossing row (including ones that fail price/time/volume
    — needed for funnel counts), each tagged with
    passes_price/passes_time/passes_volume booleans.
    """
    # _CROSSING_SQL is raw text() against trades_full/markets — unlike an
    # ORM-mapped select(), it does not trigger the session's autoflush, so
    # any pending inserts on this session (test fixtures; potentially a
    # caller building signals in the same transaction as recent writes)
    # must be flushed explicitly or they're invisible to this query.
    await session.flush()
    signal = config.signal
    result = await session.execute(
        _CROSSING_SQL,
        {
            "volume_window_hours": signal.volume_window_hours,
            "accumulation_window_minutes": signal.accumulation_window_minutes,
            "signal_min_notional_usd": signal.signal_min_notional_usd,
            "price_floor": signal.price_floor,
            "price_ceiling": signal.price_ceiling,
            "min_hours_to_resolution": signal.min_hours_to_resolution,
            "volume_floor_usd": signal.volume_floor_usd,
        },
    )
    return result.all()


async def _qualify_candidate(
    session_factory: async_sessionmaker[AsyncSession],
    row,
    config: PathfinderConfig,
    semaphore: asyncio.Semaphore,
) -> SignalRecord | None:
    detected_time = row.signal_time + timedelta(seconds=config.signal.detection_lag_seconds)
    async with semaphore:
        async with session_factory() as session:
            score = await score_wallet(session, row.wallet, detected_time, config)
    if not score.qualified:
        return None
    return SignalRecord(
        wallet=row.wallet,
        market_id=row.market_id,
        asset_id=row.asset_id,
        side=row.side,
        signal_time=row.signal_time,
        detected_time=detected_time,
        signal_price=Decimal(str(row.signal_price)),
        accumulated_notional=Decimal(str(row.accumulated_notional)),
        config_version=config.config_version,
    )


async def enumerate_signals(
    session: AsyncSession,
    session_factory: async_sessionmaker[AsyncSession],
    config: PathfinderConfig,
    concurrency: int = 30,
) -> SignalEnumerationResult:
    """Full FR-3 signal enumeration pipeline.

    `session` runs the single market-filter SQL pass. `session_factory`
    opens one session per concurrent point-in-time qualification check
    (score_wallet) — an AsyncSession is not safe for concurrent use, so
    qualification cannot share `session` (same DI pattern as
    pathfinder/booklog/snapshotter.py's take_book_snapshot).
    """
    rows = await _market_filtered_candidates(session, config)

    raw_crossings = len(rows)
    after_price = sum(1 for r in rows if r.passes_price)
    after_time = sum(1 for r in rows if r.passes_price and r.passes_time)
    market_filtered = [r for r in rows if r.passes_price and r.passes_time and r.passes_volume]

    semaphore = asyncio.Semaphore(concurrency)
    qualified_results = await asyncio.gather(
        *(_qualify_candidate(session_factory, r, config, semaphore) for r in market_filtered)
    )
    signals = [s for s in qualified_results if s is not None]

    funnel = EnumerationFunnel(
        raw_crossings=raw_crossings,
        after_price_band=after_price,
        after_time_to_resolution=after_time,
        after_volume_floor=len(market_filtered),
        after_qualification=len(signals),
        distinct_wallets_final=len({s.wallet for s in signals}),
        distinct_markets_final=len({s.market_id for s in signals}),
    )
    return SignalEnumerationResult(funnel=funnel, signals=signals)


async def materialize_signals(session: AsyncSession, signals: list[SignalRecord]) -> None:
    """Upsert SignalRecords into `signals`. Idempotent by (wallet, asset_id,
    signal_time, config_version) — re-running enumeration for the same
    config overwrites rather than duplicating (mirrors
    pathfinder/scoring/engine.py's materialize_scores)."""
    if not signals:
        return

    rows = [
        {
            "wallet": s.wallet,
            "market_id": s.market_id,
            "asset_id": s.asset_id,
            "side": s.side,
            "signal_time": s.signal_time,
            "detected_time": s.detected_time,
            "signal_price": s.signal_price,
            "accumulated_notional": s.accumulated_notional,
            "config_version": s.config_version,
            "phase": s.phase,
        }
        for s in signals
    ]

    stmt = pg_insert(Signal).values(rows)
    update_cols = {
        col: getattr(stmt.excluded, col)
        for col in (
            "market_id",
            "side",
            "detected_time",
            "signal_price",
            "accumulated_notional",
            "phase",
        )
    }
    stmt = stmt.on_conflict_do_update(
        index_elements=["wallet", "asset_id", "signal_time", "config_version"],
        set_=update_cols,
    )
    await session.execute(stmt)
