"""Markout computation (architecture §3.3, PRD FR-3/§8).

Markout at horizon h: last print at or before `signal_time + h` (staleness
cap 6h — if no print exists within the 6h preceding `signal_time + h`, the
horizon is DROPPED for that signal, never extrapolated/imputed), minus
`signal_price`. The `resolution` horizon uses the market's actual
settlement (1.0 if the signal's own outcome token won, 0.0 if it lost),
available only for markets already resolved as of when this runs — not
point-in-time gated the way scoring is, since a markout is inherently a
look-FORWARD measurement from signal_time, not a backtest input.

Deliberately does not join against the `signals` table: matched-control
events (architecture §3.3) are synthetic accumulation events from
non-qualified wallets, never persisted as `Signal` rows, and this module
computes markouts for both uniformly from bare (asset_id, signal_time,
signal_price) tuples supplied by the caller.

Gross vs net (PRD §5.6/§8): this module returns gross price-point markouts
only. Net-of-cost is a pure subtraction (`gross - cost_cents/100`) applied
at the point of use (bootstrap/report), not baked in here, since Gate 1
needs both cost tiers (1c/2c) computed from the same gross numbers.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Protocol

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

HORIZONS: dict[str, timedelta] = {
    "1h": timedelta(hours=1),
    "6h": timedelta(hours=6),
    "24h": timedelta(hours=24),
    "72h": timedelta(hours=72),
}
STALENESS_CAP = timedelta(hours=6)

_TIME_HORIZON_SQL = text(
    """
    WITH ev AS (
        SELECT * FROM unnest(:ids ::text[], :asset_ids ::text[], :signal_times ::timestamptz[])
            AS t(id, asset_id, signal_time)
    )
    SELECT ev.id, h.label, p.price AS price_at_h
    FROM ev
    CROSS JOIN (
        SELECT unnest(:horizon_labels ::text[]) AS label, unnest(:horizon_seconds ::int[]) AS seconds
    ) h
    LEFT JOIN LATERAL (
        SELECT tf.price
        FROM trades_full tf
        WHERE tf.asset_id = ev.asset_id
          AND tf.price IS NOT NULL
          AND tf.ts <= ev.signal_time + make_interval(secs => h.seconds)
          AND tf.ts >= ev.signal_time + make_interval(secs => h.seconds) - make_interval(secs => :staleness_cap_seconds)
        ORDER BY tf.ts DESC
        LIMIT 1
    ) p ON true
    """
)

_RESOLUTION_SQL = text(
    """
    WITH ev AS (
        SELECT * FROM unnest(:ids ::text[], :market_ids ::text[], :asset_ids ::text[])
            AS t(id, market_id, asset_id)
    )
    SELECT ev.id,
        CASE WHEN m.resolved AND t.outcome = m.resolution THEN 1.0
             WHEN m.resolved THEN 0.0
             ELSE NULL END AS price_at_h
    FROM ev
    JOIN markets m ON m.market_id = ev.market_id
    LEFT JOIN tokens t ON t.asset_id = ev.asset_id
    """
)


class MarkoutEvent(Protocol):
    id: str
    wallet: str
    market_id: str
    asset_id: str
    signal_time: datetime
    signal_price: Decimal | float


@dataclass(frozen=True)
class SignalMarkout:
    signal_id: str
    wallet: str
    market_id: str
    asset_id: str
    signal_time: datetime
    signal_price: Decimal
    horizon_prices: dict[str, Decimal | None]

    def gross_markout(self, horizon: str) -> Decimal | None:
        price = self.horizon_prices.get(horizon)
        if price is None:
            return None
        return price - self.signal_price

    def net_markout(self, horizon: str, cost_cents: float) -> Decimal | None:
        gross = self.gross_markout(horizon)
        if gross is None:
            return None
        return gross - Decimal(str(cost_cents)) / Decimal(100)


async def compute_markouts(
    session: AsyncSession,
    events: list[MarkoutEvent],
    include_resolution: bool = True,
) -> list[SignalMarkout]:
    """events: any objects with .id (caller-assigned, unique within this
    call — not required to exist in the `signals` table), .wallet,
    .market_id, .asset_id, .signal_time, .signal_price. Works uniformly for
    real Signal rows and synthetic matched-control events."""
    by_id = {e.id: e for e in events}
    if not by_id:
        return []
    ids = list(by_id.keys())

    # Raw text() queries don't trigger session autoflush (see
    # pathfinder/research/signals.py's _market_filtered_candidates for the
    # same guard and full explanation).
    await session.flush()

    horizon_prices: dict[str, dict[str, Decimal | None]] = {eid: {} for eid in ids}

    time_result = await session.execute(
        _TIME_HORIZON_SQL,
        {
            "ids": ids,
            "asset_ids": [by_id[eid].asset_id for eid in ids],
            "signal_times": [by_id[eid].signal_time for eid in ids],
            "horizon_labels": list(HORIZONS.keys()),
            "horizon_seconds": [int(td.total_seconds()) for td in HORIZONS.values()],
            "staleness_cap_seconds": int(STALENESS_CAP.total_seconds()),
        },
    )
    for eid, label, price in time_result.all():
        horizon_prices[eid][label] = Decimal(str(price)) if price is not None else None

    if include_resolution:
        res_result = await session.execute(
            _RESOLUTION_SQL,
            {
                "ids": ids,
                "market_ids": [by_id[eid].market_id for eid in ids],
                "asset_ids": [by_id[eid].asset_id for eid in ids],
            },
        )
        for eid, price in res_result.all():
            horizon_prices[eid]["resolution"] = Decimal(str(price)) if price is not None else None

    return [
        SignalMarkout(
            signal_id=eid,
            wallet=row.wallet,
            market_id=row.market_id,
            asset_id=row.asset_id,
            signal_time=row.signal_time,
            signal_price=Decimal(str(row.signal_price)),
            horizon_prices=horizon_prices[eid],
        )
        for eid, row in by_id.items()
    ]
