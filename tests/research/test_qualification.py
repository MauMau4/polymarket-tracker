"""End-to-end enumerate_signals: point-in-time wallet qualification gating.

Confirms qualification uses score_wallet (the one M1 scoring code path, per
architecture design principle #2) and, specifically, that it is evaluated
`as_of=detected_time` (signal_time + detection_lag) rather than
`signal_time` itself — the judgment call documented in
pathfinder/research/signals.py.

The qualification stage opens its own sessions via `session_factory` (an
AsyncSession isn't safe for concurrent use), a different DB connection than
`db_session` — data seeded via `db_session` must be committed before
`enumerate_signals` is called, or the qualification-stage sessions won't see
it (same pattern as tests/booklog/test_snapshotter.py).
"""
from datetime import datetime, timedelta, timezone

import pytest

from pathfinder.config import load_config
from pathfinder.research.signals import enumerate_signals
from tests.research.conftest import add_filler_volume, add_market, add_trade
from tests.scoring.conftest import add_wallet, add_wcp

pytestmark = pytest.mark.asyncio

CONFIG = load_config()
T0 = datetime(2026, 6, 1, 12, 0, tzinfo=timezone.utc)
DETECTED = T0 + timedelta(seconds=CONFIG.signal.detection_lag_seconds)


async def _seed_signal_trigger(db_session, wallet: str):
    """A crossing that passes every market filter (price/time/volume)."""
    await add_market(db_session, market_id="m1", end_date=T0 + timedelta(days=30))
    await add_filler_volume(db_session, market_id="m1", ts=T0 - timedelta(hours=1), total_notional=10000)
    await add_trade(db_session, wallet=wallet, asset_id="a1", market_id="m1", ts=T0, price=0.5, size=1200, side="BUY")


async def _seed_qualifying_positions(db_session, wallet: str, n: int, closed_at):
    for i in range(n):
        await add_wcp(
            db_session, wallet=wallet, closed_at=closed_at - timedelta(hours=i),
            entry_price=0.30, exit_price=0.80, shares_sold=100.0,
        )


async def test_unqualified_wallet_yields_no_final_signal(db_session, session_factory):
    wallet = "0xunqualified"
    await add_wallet(db_session, wallet=wallet)
    await _seed_signal_trigger(db_session, wallet)
    # Only 5 closed positions -- fails min_closed_positions (20).
    await _seed_qualifying_positions(db_session, wallet, 5, T0 - timedelta(days=10))
    await db_session.commit()

    result = await enumerate_signals(db_session, session_factory, CONFIG)

    assert result.funnel.after_volume_floor == 1
    assert result.funnel.after_qualification == 0
    assert result.signals == []


async def test_qualified_wallet_yields_final_signal(db_session, session_factory):
    wallet = "0xqualified"
    await add_wallet(db_session, wallet=wallet)
    await _seed_signal_trigger(db_session, wallet)
    await _seed_qualifying_positions(db_session, wallet, 20, T0 - timedelta(days=10))
    await db_session.commit()

    result = await enumerate_signals(db_session, session_factory, CONFIG)

    assert result.funnel.after_qualification == 1
    assert len(result.signals) == 1
    sig = result.signals[0]
    assert sig.wallet == wallet
    assert sig.signal_time == T0
    assert sig.detected_time == DETECTED
    assert sig.config_version == CONFIG.config_version


async def test_qualification_evaluated_at_detected_time_not_signal_time(db_session, session_factory):
    wallet = "0xlagsensitive"
    await add_wallet(db_session, wallet=wallet)
    await _seed_signal_trigger(db_session, wallet)
    # 19 positions safely closed before signal_time...
    await _seed_qualifying_positions(db_session, wallet, 19, T0 - timedelta(days=10))
    # ...and a 20th closed strictly between signal_time and detected_time.
    # Using as_of=signal_time would see only 19 (disqualified); the correct
    # as_of=detected_time sees all 20 (qualified).
    straggler_closed_at = T0 + timedelta(seconds=CONFIG.signal.detection_lag_seconds / 2)
    assert T0 < straggler_closed_at < DETECTED
    await add_wcp(
        db_session, wallet=wallet, closed_at=straggler_closed_at,
        entry_price=0.30, exit_price=0.80, shares_sold=100.0,
    )
    await db_session.commit()

    result = await enumerate_signals(db_session, session_factory, CONFIG)

    assert result.funnel.after_qualification == 1
    assert len(result.signals) == 1
