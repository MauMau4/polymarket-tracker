"""Matched control construction (architecture §3.3).

"Matched control: same market, same notional band (+/-50%), non-qualified
wallet, nearest-in-time. One control per signal; sampling seeded."

The control pool is built from the SAME rolling-window crossing-detection
logic as pathfinder/research/signals.py, restricted to the specific markets
that have a real signal, but deliberately WITHOUT the PRD §5.2 eligibility
filters (price band / time-to-resolution / volume floor) — those are
signal-eligibility rules for the strategy under test, not a property a
"random large trade" control needs to satisfy; the control's only job is to
answer "does a same-size trade in the same market move price this much
even from a wallet with no demonstrated skill?" The notional floor for
control detection is also lowered relative to signal_min_notional_usd,
since a signal's -50% band can fall below it for signals near the floor.

Nearest-in-time ties are broken by a seeded RNG (`random.Random(seed)`) —
the PRD says "sampling seeded" but names no seed; 42 is used and logged
(decisions/2026-07-17.md), consistent with the M1 precedent of logging
underspecified-PRD judgment calls.
"""
from __future__ import annotations

import asyncio
import random
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from pathfinder.config import PathfinderConfig
from pathfinder.scoring.engine import score_wallet

DEFAULT_SEED = 42
NOTIONAL_BAND = Decimal("0.5")  # +/-50%

_CONTROL_POOL_SQL = text(
    """
    WITH trade_base AS (
        SELECT wallet, asset_id, market_id, ts, price, notional_usd, side
        FROM trades_full
        WHERE notional_usd IS NOT NULL AND side IN ('BUY', 'SELL') AND price IS NOT NULL
          AND wallet IS NOT NULL AND market_id = ANY(:market_ids ::text[])
    ),
    rolling AS (
        SELECT *,
            SUM(CASE WHEN side = 'BUY' THEN notional_usd ELSE -notional_usd END) OVER (
                PARTITION BY wallet, asset_id ORDER BY ts
                RANGE BETWEEN make_interval(mins => :accumulation_window_minutes) PRECEDING AND CURRENT ROW
            ) AS net_notional
        FROM trade_base
    ),
    crossing AS (
        SELECT *,
            LAG(net_notional) OVER (PARTITION BY wallet, asset_id ORDER BY ts) AS prev_net_notional
        FROM rolling
    )
    SELECT wallet, asset_id, market_id, ts AS signal_time, price AS signal_price, net_notional AS accumulated_notional
    FROM crossing
    WHERE net_notional >= :control_min_notional
      AND (prev_net_notional IS NULL OR prev_net_notional < :control_min_notional)
    ORDER BY wallet, asset_id, signal_time
    """
)


@dataclass(frozen=True)
class ControlCandidate:
    wallet: str
    market_id: str
    asset_id: str
    signal_time: datetime
    signal_price: Decimal
    accumulated_notional: Decimal


@dataclass(frozen=True)
class ControlMatch:
    signal_id: str
    candidate: ControlCandidate


async def _control_pool(
    session: AsyncSession, market_ids: set[str], control_min_notional: Decimal, config: PathfinderConfig
) -> list[ControlCandidate]:
    await session.flush()
    result = await session.execute(
        _CONTROL_POOL_SQL,
        {
            "market_ids": list(market_ids),
            "accumulation_window_minutes": config.signal.accumulation_window_minutes,
            "control_min_notional": control_min_notional,
        },
    )
    return [
        ControlCandidate(
            wallet=wallet, market_id=market_id, asset_id=asset_id, signal_time=signal_time,
            signal_price=Decimal(str(signal_price)), accumulated_notional=Decimal(str(accumulated_notional)),
        )
        for wallet, asset_id, market_id, signal_time, signal_price, accumulated_notional in result.all()
    ]


async def find_matched_controls(
    session: AsyncSession,
    session_factory: async_sessionmaker[AsyncSession],
    signals: list,
    config: PathfinderConfig,
    seed: int = DEFAULT_SEED,
    concurrency: int = 30,
) -> dict[str, ControlMatch | None]:
    """signals: Signal rows (or duck-typed equivalents) with .id, .wallet,
    .market_id, .accumulated_notional, .signal_time. Returns a mapping
    signal.id -> ControlMatch, or None if no eligible control was found."""
    if not signals:
        return {}

    market_ids = {s.market_id for s in signals}
    control_min_notional = min(Decimal(str(s.accumulated_notional)) for s in signals) * (1 - NOTIONAL_BAND)

    pool = await _control_pool(session, market_ids, control_min_notional, config)

    semaphore = asyncio.Semaphore(concurrency)

    async def _is_qualified(candidate: ControlCandidate) -> bool:
        # Evaluated at this exact candidate's own as_of (signal_time +
        # detection_lag) -- same rigor as enumerate_signals' per-instant
        # qualification check, not a coarser per-wallet flag (a wallet can
        # be qualified at one point in the pool and not another).
        as_of = candidate.signal_time + timedelta(seconds=config.signal.detection_lag_seconds)
        async with semaphore:
            async with session_factory() as s:
                score = await score_wallet(s, candidate.wallet, as_of, config)
        return score.qualified

    qualified_flags = await asyncio.gather(*(_is_qualified(c) for c in pool))
    non_qualified_pool = [c for c, qualified in zip(pool, qualified_flags) if not qualified]

    rng = random.Random(seed)
    matches: dict[str, ControlMatch | None] = {}
    for s in signals:
        notional = Decimal(str(s.accumulated_notional))
        lo, hi = notional * (1 - NOTIONAL_BAND), notional * (1 + NOTIONAL_BAND)
        eligible = [
            c for c in non_qualified_pool
            if c.market_id == s.market_id and lo <= c.accumulated_notional <= hi and c.wallet != s.wallet
        ]
        if not eligible:
            matches[s.id] = None
            continue
        min_delta = min(abs((c.signal_time - s.signal_time).total_seconds()) for c in eligible)
        nearest = [c for c in eligible if abs((c.signal_time - s.signal_time).total_seconds()) == min_delta]
        chosen = nearest[0] if len(nearest) == 1 else rng.choice(nearest)
        matches[s.id] = ControlMatch(signal_id=s.id, candidate=chosen)

    return matches
