"""Markout computation (architecture §3.3): staleness cap, gross/net, resolution."""
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from pathfinder.research.markouts import compute_markouts
from tests.research.conftest import add_market, add_token, add_trade

pytestmark = pytest.mark.asyncio

T0 = datetime(2026, 6, 1, 12, 0, tzinfo=timezone.utc)


@dataclass
class _Event:
    id: str
    wallet: str
    market_id: str
    asset_id: str
    signal_time: datetime
    signal_price: Decimal


async def test_markout_uses_last_print_at_or_before_horizon(db_session):
    await add_market(db_session, market_id="m1", end_date=None)
    # Print at +5h (before the +6h horizon) should be picked up.
    await add_trade(db_session, wallet="0xany", asset_id="a1", market_id="m1", ts=T0 + timedelta(hours=5), price=0.65, size=10, side="BUY")

    events = [_Event(id="e1", wallet="0xw1", market_id="m1", asset_id="a1", signal_time=T0, signal_price=Decimal("0.5"))]
    results = await compute_markouts(db_session, events, include_resolution=False)

    assert len(results) == 1
    assert results[0].horizon_prices["6h"] == Decimal("0.65")
    assert results[0].gross_markout("6h") == Decimal("0.15")


async def test_markout_ignores_prints_after_horizon(db_session):
    await add_market(db_session, market_id="m1", end_date=None)
    # Only print is AFTER the +1h horizon -- must not be used for the 1h markout.
    await add_trade(db_session, wallet="0xany", asset_id="a1", market_id="m1", ts=T0 + timedelta(hours=2), price=0.9, size=10, side="BUY")

    events = [_Event(id="e1", wallet="0xw1", market_id="m1", asset_id="a1", signal_time=T0, signal_price=Decimal("0.5"))]
    results = await compute_markouts(db_session, events, include_resolution=False)

    assert results[0].horizon_prices["1h"] is None
    assert results[0].gross_markout("1h") is None


async def test_staleness_cap_drops_horizon_when_no_recent_print(db_session):
    await add_market(db_session, market_id="m1", end_date=None)
    # Last print is 7h before the +24h horizon -- exceeds the 6h staleness cap.
    await add_trade(
        db_session, wallet="0xany", asset_id="a1", market_id="m1",
        ts=T0 + timedelta(hours=24) - timedelta(hours=7), price=0.6, size=10, side="BUY",
    )

    events = [_Event(id="e1", wallet="0xw1", market_id="m1", asset_id="a1", signal_time=T0, signal_price=Decimal("0.5"))]
    results = await compute_markouts(db_session, events, include_resolution=False)

    assert results[0].horizon_prices["24h"] is None


async def test_print_exactly_at_staleness_cap_boundary_is_included(db_session):
    await add_market(db_session, market_id="m1", end_date=None)
    # Exactly 6h before the +24h horizon -- boundary is inclusive (>=).
    await add_trade(
        db_session, wallet="0xany", asset_id="a1", market_id="m1",
        ts=T0 + timedelta(hours=24) - timedelta(hours=6), price=0.6, size=10, side="BUY",
    )

    events = [_Event(id="e1", wallet="0xw1", market_id="m1", asset_id="a1", signal_time=T0, signal_price=Decimal("0.5"))]
    results = await compute_markouts(db_session, events, include_resolution=False)

    assert results[0].horizon_prices["24h"] == Decimal("0.6")


async def test_net_markout_subtracts_cost(db_session):
    await add_market(db_session, market_id="m1", end_date=None)
    await add_trade(db_session, wallet="0xany", asset_id="a1", market_id="m1", ts=T0 + timedelta(hours=1), price=0.55, size=10, side="BUY")

    events = [_Event(id="e1", wallet="0xw1", market_id="m1", asset_id="a1", signal_time=T0, signal_price=Decimal("0.5"))]
    results = await compute_markouts(db_session, events, include_resolution=False)

    assert results[0].net_markout("1h", cost_cents=1) == Decimal("0.05") - Decimal("0.01")
    assert results[0].net_markout("1h", cost_cents=2) == Decimal("0.05") - Decimal("0.02")


async def test_resolution_markout_winning_outcome(db_session):
    await add_market(db_session, market_id="m1", end_date=None, resolved=True, resolution="Yes")
    await add_token(db_session, market_id="m1", asset_id="a1", outcome="Yes")

    events = [_Event(id="e1", wallet="0xw1", market_id="m1", asset_id="a1", signal_time=T0, signal_price=Decimal("0.5"))]
    results = await compute_markouts(db_session, events, include_resolution=True)

    assert results[0].horizon_prices["resolution"] == Decimal("1.0")
    assert results[0].gross_markout("resolution") == Decimal("0.5")


async def test_resolution_markout_losing_outcome(db_session):
    await add_market(db_session, market_id="m1", end_date=None, resolved=True, resolution="No")
    await add_token(db_session, market_id="m1", asset_id="a1", outcome="Yes")

    events = [_Event(id="e1", wallet="0xw1", market_id="m1", asset_id="a1", signal_time=T0, signal_price=Decimal("0.5"))]
    results = await compute_markouts(db_session, events, include_resolution=True)

    assert results[0].horizon_prices["resolution"] == Decimal("0.0")
    assert results[0].gross_markout("resolution") == Decimal("-0.5")


async def test_resolution_markout_dropped_when_market_unresolved(db_session):
    await add_market(db_session, market_id="m1", end_date=None, resolved=False)
    await add_token(db_session, market_id="m1", asset_id="a1", outcome="Yes")

    events = [_Event(id="e1", wallet="0xw1", market_id="m1", asset_id="a1", signal_time=T0, signal_price=Decimal("0.5"))]
    results = await compute_markouts(db_session, events, include_resolution=True)

    assert results[0].horizon_prices["resolution"] is None


async def test_multiple_events_independent(db_session):
    await add_market(db_session, market_id="m1", end_date=None)
    await add_trade(db_session, wallet="0xany", asset_id="a1", market_id="m1", ts=T0 + timedelta(hours=1), price=0.6, size=10, side="BUY")
    await add_trade(db_session, wallet="0xany", asset_id="a2", market_id="m1", ts=T0 + timedelta(hours=1), price=0.3, size=10, side="BUY")

    events = [
        _Event(id="e1", wallet="0xw1", market_id="m1", asset_id="a1", signal_time=T0, signal_price=Decimal("0.5")),
        _Event(id="e2", wallet="0xw2", market_id="m1", asset_id="a2", signal_time=T0, signal_price=Decimal("0.4")),
    ]
    results = await compute_markouts(db_session, events, include_resolution=False)
    by_id = {r.signal_id: r for r in results}

    assert by_id["e1"].gross_markout("1h") == Decimal("0.1")
    assert by_id["e2"].gross_markout("1h") == Decimal("-0.1")
