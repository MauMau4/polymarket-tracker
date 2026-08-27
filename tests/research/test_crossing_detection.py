"""Rolling-window accumulation crossing detection (PRD §5.2, architecture §3.3).

Tests the "rising edge" firing semantics documented in
pathfinder/research/signals.py: a signal fires the first time a wallet's
trailing-window net notional on one asset_id crosses from below
signal_min_notional_usd to at-or-above it, and only re-fires after
dropping back below and re-crossing.

These tests inspect raw crossing rows (ignoring price/time/volume flags —
covered separately in test_market_filters.py) via the private
_market_filtered_candidates() SQL pass; a market row is still required
since the SQL joins to `markets` to compute the time-to-resolution flag.
"""
from datetime import datetime, timedelta, timezone

import pytest

from pathfinder.config import load_config
from pathfinder.research.signals import _market_filtered_candidates
from tests.research.conftest import add_market, add_trade

pytestmark = pytest.mark.asyncio

CONFIG = load_config()
T0 = datetime(2026, 6, 1, 12, 0, tzinfo=timezone.utc)


async def _rows(db_session):
    return await _market_filtered_candidates(db_session, CONFIG)


async def test_single_large_buy_fires_once(db_session):
    await add_market(db_session, market_id="m1", end_date=T0 + timedelta(days=30))
    await add_trade(db_session, wallet="0xw1", asset_id="a1", market_id="m1", ts=T0, price=0.5, size=1200, side="BUY")

    rows = await _rows(db_session)

    assert len(rows) == 1
    assert rows[0].wallet == "0xw1"
    assert float(rows[0].accumulated_notional) == pytest.approx(600.0)
    assert float(rows[0].signal_price) == pytest.approx(0.5)


async def test_two_buys_summing_to_threshold_fires_once_at_second_trade(db_session):
    await add_market(db_session, market_id="m1", end_date=T0 + timedelta(days=30))
    # 300 + 300 = 600 >= 500, 5 min apart (within the 60-min window).
    await add_trade(db_session, wallet="0xw1", asset_id="a1", market_id="m1", ts=T0, price=0.5, size=600, side="BUY")
    await add_trade(
        db_session, wallet="0xw1", asset_id="a1", market_id="m1",
        ts=T0 + timedelta(minutes=5), price=0.55, size=545.45, side="BUY",
    )

    rows = await _rows(db_session)

    assert len(rows) == 1
    assert rows[0].signal_time == T0 + timedelta(minutes=5)
    assert float(rows[0].signal_price) == pytest.approx(0.55)


async def test_continued_buys_after_crossing_do_not_refire(db_session):
    await add_market(db_session, market_id="m1", end_date=T0 + timedelta(days=30))
    for i, minutes in enumerate([0, 5, 10]):
        await add_trade(
            db_session, wallet="0xw1", asset_id="a1", market_id="m1",
            ts=T0 + timedelta(minutes=minutes), price=0.5, size=600, side="BUY",
        )

    rows = await _rows(db_session)

    # Trade 1 alone: net=300 < 500, no signal.
    # Trade 2: net=600 >= 500, crosses -> 1 signal.
    # Trade 3: net=900 >= 500, but prev (600) was already >= 500 -> no new signal.
    assert len(rows) == 1
    assert rows[0].signal_time == T0 + timedelta(minutes=5)


async def test_drop_below_threshold_then_recross_fires_again(db_session):
    await add_market(db_session, market_id="m1", end_date=T0 + timedelta(days=30))
    await add_trade(db_session, wallet="0xw1", asset_id="a1", market_id="m1", ts=T0, price=0.5, size=1200, side="BUY")
    # net drops 600 -> 200 (SELL 400) -- still < 500, no new crossing.
    await add_trade(
        db_session, wallet="0xw1", asset_id="a1", market_id="m1",
        ts=T0 + timedelta(minutes=10), price=0.5, size=800, side="SELL",
    )
    # net rises 200 -> 700 (BUY 500) -- re-crosses 500.
    await add_trade(
        db_session, wallet="0xw1", asset_id="a1", market_id="m1",
        ts=T0 + timedelta(minutes=20), price=0.5, size=1000, side="BUY",
    )

    rows = await _rows(db_session)

    assert len(rows) == 2
    assert [r.signal_time for r in rows] == [T0, T0 + timedelta(minutes=20)]


async def test_sell_dominant_never_fires(db_session):
    await add_market(db_session, market_id="m1", end_date=T0 + timedelta(days=30))
    await add_trade(db_session, wallet="0xw1", asset_id="a1", market_id="m1", ts=T0, price=0.5, size=2000, side="SELL")

    rows = await _rows(db_session)

    assert rows == []


async def test_trades_outside_accumulation_window_do_not_accumulate(db_session):
    await add_market(db_session, market_id="m1", end_date=T0 + timedelta(days=30))
    await add_trade(db_session, wallet="0xw1", asset_id="a1", market_id="m1", ts=T0, price=0.5, size=600, side="BUY")
    await add_trade(
        db_session, wallet="0xw1", asset_id="a1", market_id="m1",
        ts=T0 + timedelta(minutes=61), price=0.5, size=600, side="BUY",
    )

    rows = await _rows(db_session)

    assert rows == []


async def test_partitioned_independently_by_wallet_and_asset(db_session):
    await add_market(db_session, market_id="m1", end_date=T0 + timedelta(days=30))
    await add_market(db_session, market_id="m2", end_date=T0 + timedelta(days=30))
    # Each trade crosses on its own (notional=600); partitioning must not sum
    # them together across wallets or across assets.
    await add_trade(db_session, wallet="0xw1", asset_id="a1", market_id="m1", ts=T0, price=0.5, size=1200, side="BUY")
    await add_trade(db_session, wallet="0xw2", asset_id="a1", market_id="m1", ts=T0, price=0.5, size=1200, side="BUY")
    # Same wallet, different asset (opposite outcome token of a different market): independent too.
    await add_trade(db_session, wallet="0xw1", asset_id="a2", market_id="m2", ts=T0, price=0.5, size=1200, side="BUY")

    rows = await _rows(db_session)

    assert len(rows) == 3
    assert {(r.wallet, r.asset_id) for r in rows} == {("0xw1", "a1"), ("0xw2", "a1"), ("0xw1", "a2")}
