"""Matched control construction (architecture §3.3): notional band, non-qualified
wallet requirement, nearest-in-time selection."""
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from pathfinder.config import load_config
from pathfinder.research.controls import find_matched_controls
from tests.research.conftest import add_market, add_trade
from tests.scoring.conftest import add_wallet, add_wcp

pytestmark = pytest.mark.asyncio

CONFIG = load_config()
T0 = datetime(2026, 6, 1, 12, 0, tzinfo=timezone.utc)


@dataclass
class _Signal:
    id: str
    wallet: str
    market_id: str
    signal_time: datetime
    accumulated_notional: Decimal


async def _qualify_wallet(db_session, wallet: str, before: datetime):
    """20 uniform qualifying positions, well before `before`."""
    await add_wallet(db_session, wallet=wallet)
    for i in range(20):
        await add_wcp(
            db_session, wallet=wallet, closed_at=before - timedelta(days=1, hours=i),
            entry_price=0.30, exit_price=0.80, shares_sold=100.0,
        )


async def test_matches_nearest_in_time_within_band(db_session, session_factory):
    await add_market(db_session, market_id="m1", end_date=None)
    await _qualify_wallet(db_session, "0xsignalwallet", T0)
    # Non-qualified control candidates in-band at different distances from T0.
    await add_trade(db_session, wallet="0xctrl_far", asset_id="a2", market_id="m1", ts=T0 - timedelta(hours=10), price=0.5, size=1000, side="BUY")
    await add_trade(db_session, wallet="0xctrl_near", asset_id="a3", market_id="m1", ts=T0 + timedelta(hours=1), price=0.5, size=1000, side="BUY")
    await db_session.commit()

    signals = [_Signal(id="s1", wallet="0xsignalwallet", market_id="m1", signal_time=T0, accumulated_notional=Decimal("500"))]
    matches = await find_matched_controls(db_session, session_factory, signals, CONFIG)

    assert matches["s1"] is not None
    assert matches["s1"].candidate.wallet == "0xctrl_near"


async def test_candidate_outside_notional_band_excluded(db_session, session_factory):
    await add_market(db_session, market_id="m1", end_date=None)
    await _qualify_wallet(db_session, "0xsignalwallet", T0)
    # Signal notional 500 -> band [250, 750]. This candidate's notional (1000) is outside.
    await add_trade(db_session, wallet="0xctrl_toolarge", asset_id="a2", market_id="m1", ts=T0 + timedelta(hours=1), price=0.5, size=2000, side="BUY")
    await db_session.commit()

    signals = [_Signal(id="s1", wallet="0xsignalwallet", market_id="m1", signal_time=T0, accumulated_notional=Decimal("500"))]
    matches = await find_matched_controls(db_session, session_factory, signals, CONFIG)

    assert matches["s1"] is None


async def test_candidate_at_band_edge_included(db_session, session_factory):
    await add_market(db_session, market_id="m1", end_date=None)
    await _qualify_wallet(db_session, "0xsignalwallet", T0)
    # Signal notional 500 -> band [250, 750]. Exactly 750 (upper edge) is inclusive.
    await add_trade(db_session, wallet="0xctrl_edge", asset_id="a2", market_id="m1", ts=T0 + timedelta(hours=1), price=0.5, size=1500, side="BUY")
    await db_session.commit()

    signals = [_Signal(id="s1", wallet="0xsignalwallet", market_id="m1", signal_time=T0, accumulated_notional=Decimal("500"))]
    matches = await find_matched_controls(db_session, session_factory, signals, CONFIG)

    assert matches["s1"] is not None
    assert matches["s1"].candidate.wallet == "0xctrl_edge"


async def test_qualified_wallet_never_used_as_control(db_session, session_factory):
    await add_market(db_session, market_id="m1", end_date=None)
    await _qualify_wallet(db_session, "0xsignalwallet", T0)
    # This wallet is ALSO qualified (20 qualifying positions) -- must be excluded
    # from the control pool even though it's the nearest in-band candidate.
    await _qualify_wallet(db_session, "0xalso_qualified", T0)
    await add_trade(db_session, wallet="0xalso_qualified", asset_id="a2", market_id="m1", ts=T0 + timedelta(hours=1), price=0.5, size=1000, side="BUY")
    # A genuinely non-qualified, farther candidate should be chosen instead.
    await add_trade(db_session, wallet="0xctrl_unqualified", asset_id="a3", market_id="m1", ts=T0 + timedelta(hours=5), price=0.5, size=1000, side="BUY")
    await db_session.commit()

    signals = [_Signal(id="s1", wallet="0xsignalwallet", market_id="m1", signal_time=T0, accumulated_notional=Decimal("500"))]
    matches = await find_matched_controls(db_session, session_factory, signals, CONFIG)

    assert matches["s1"] is not None
    assert matches["s1"].candidate.wallet == "0xctrl_unqualified"


async def test_different_market_excluded(db_session, session_factory):
    await add_market(db_session, market_id="m1", end_date=None)
    await add_market(db_session, market_id="m2", end_date=None)
    await _qualify_wallet(db_session, "0xsignalwallet", T0)
    await add_trade(db_session, wallet="0xctrl_wrongmarket", asset_id="a2", market_id="m2", ts=T0 + timedelta(hours=1), price=0.5, size=1000, side="BUY")
    await db_session.commit()

    signals = [_Signal(id="s1", wallet="0xsignalwallet", market_id="m1", signal_time=T0, accumulated_notional=Decimal("500"))]
    matches = await find_matched_controls(db_session, session_factory, signals, CONFIG)

    assert matches["s1"] is None


async def test_no_eligible_candidate_yields_none(db_session, session_factory):
    await add_market(db_session, market_id="m1", end_date=None)
    await _qualify_wallet(db_session, "0xsignalwallet", T0)
    await db_session.commit()

    signals = [_Signal(id="s1", wallet="0xsignalwallet", market_id="m1", signal_time=T0, accumulated_notional=Decimal("500"))]
    matches = await find_matched_controls(db_session, session_factory, signals, CONFIG)

    assert matches["s1"] is None
