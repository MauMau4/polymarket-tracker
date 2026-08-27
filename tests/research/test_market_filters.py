"""Price band / time-to-resolution / volume-floor filters (PRD §5.2).

_market_filtered_candidates() returns every crossing row joined to its
market with passes_price/passes_time/passes_volume flags attached (no
WHERE filtering in SQL) — these tests assert the flag values directly, one
condition at a time.
"""
from datetime import datetime, timedelta, timezone

import pytest

from pathfinder.config import load_config
from pathfinder.research.signals import _market_filtered_candidates
from tests.research.conftest import add_filler_volume, add_market, add_trade

pytestmark = pytest.mark.asyncio

CONFIG = load_config()
T0 = datetime(2026, 6, 1, 12, 0, tzinfo=timezone.utc)


async def _one_row(db_session):
    rows = await _market_filtered_candidates(db_session, CONFIG)
    assert len(rows) == 1
    return rows[0]


async def test_price_above_ceiling_fails_price_band(db_session):
    await add_market(db_session, market_id="m1", end_date=T0 + timedelta(days=30))
    await add_trade(db_session, wallet="0xw1", asset_id="a1", market_id="m1", ts=T0, price=0.95, size=600, side="BUY")

    row = await _one_row(db_session)

    assert row.passes_price is False


async def test_price_below_floor_fails_price_band(db_session):
    await add_market(db_session, market_id="m1", end_date=T0 + timedelta(days=30))
    await add_trade(db_session, wallet="0xw1", asset_id="a1", market_id="m1", ts=T0, price=0.03, size=20000, side="BUY")

    row = await _one_row(db_session)

    assert row.passes_price is False


async def test_price_in_band_passes(db_session):
    await add_market(db_session, market_id="m1", end_date=T0 + timedelta(days=30))
    await add_trade(db_session, wallet="0xw1", asset_id="a1", market_id="m1", ts=T0, price=0.5, size=1200, side="BUY")

    row = await _one_row(db_session)

    assert row.passes_price is True


async def test_null_end_date_fails_time_to_resolution(db_session):
    await add_market(db_session, market_id="m1", end_date=None)
    await add_trade(db_session, wallet="0xw1", asset_id="a1", market_id="m1", ts=T0, price=0.5, size=1200, side="BUY")

    row = await _one_row(db_session)

    assert row.passes_time is False


async def test_less_than_24h_to_resolution_fails(db_session):
    await add_market(db_session, market_id="m1", end_date=T0 + timedelta(hours=23))
    await add_trade(db_session, wallet="0xw1", asset_id="a1", market_id="m1", ts=T0, price=0.5, size=1200, side="BUY")

    row = await _one_row(db_session)

    assert row.passes_time is False


async def test_exactly_24h_to_resolution_passes(db_session):
    await add_market(db_session, market_id="m1", end_date=T0 + timedelta(hours=24))
    await add_trade(db_session, wallet="0xw1", asset_id="a1", market_id="m1", ts=T0, price=0.5, size=1200, side="BUY")

    row = await _one_row(db_session)

    assert row.passes_time is True


async def test_low_market_volume_fails_volume_floor(db_session):
    await add_market(db_session, market_id="m1", end_date=T0 + timedelta(days=30))
    # Only the signal's own trade contributes volume: $600 << $10,000 floor.
    await add_trade(db_session, wallet="0xw1", asset_id="a1", market_id="m1", ts=T0, price=0.5, size=1200, side="BUY")

    row = await _one_row(db_session)

    assert row.passes_volume is False


async def test_sufficient_trailing_24h_volume_passes(db_session):
    await add_market(db_session, market_id="m1", end_date=T0 + timedelta(days=30))
    # Filler volume (many distinct one-shot wallets, each under the
    # accumulation threshold) within the trailing 24h window pushes
    # market_trailing_volume over $10,000 without producing extra signals.
    await add_filler_volume(db_session, market_id="m1", ts=T0 - timedelta(hours=1), total_notional=10000)
    await add_trade(db_session, wallet="0xw1", asset_id="a1", market_id="m1", ts=T0, price=0.5, size=1200, side="BUY")

    row = await _one_row(db_session)

    assert row.passes_volume is True


async def test_filler_volume_outside_24h_window_excluded(db_session):
    await add_market(db_session, market_id="m1", end_date=T0 + timedelta(days=30))
    # Filler volume 25h before the signal is outside the trailing 24h window.
    await add_filler_volume(db_session, market_id="m1", ts=T0 - timedelta(hours=25), total_notional=10000)
    await add_trade(db_session, wallet="0xw1", asset_id="a1", market_id="m1", ts=T0, price=0.5, size=1200, side="BUY")

    row = await _one_row(db_session)

    assert row.passes_volume is False
