"""PERMANENT, NON-NEGOTIABLE. Do not skip or weaken (pathfinder/CLAUDE.md
rule 2; architecture §3.1 invariant). Proves a position closed AFTER as_of
can never influence a score computed for that as_of, for both score_wallet
and score_universe.
"""
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from pathfinder.config import load_config
from pathfinder.scoring.engine import score_universe, score_wallet
from tests.scoring.conftest import add_wallet, add_wcp

pytestmark = pytest.mark.asyncio

CONFIG = load_config()
AS_OF = datetime(2026, 6, 1, tzinfo=timezone.utc)


async def test_future_position_excluded_from_score_wallet(db_session):
    wallet = "0xfuture1"
    await add_wallet(db_session, wallet=wallet)
    # Legitimate, in-window position: should count.
    await add_wcp(
        db_session,
        wallet=wallet,
        closed_at=AS_OF - timedelta(days=10),
        entry_price=0.30,
        exit_price=0.80,
    )
    # Planted future position: closed AFTER as_of. If this leaked in, n_positions
    # would be 2 and mean_roi would be dragged toward a near-total loss.
    await add_wcp(
        db_session,
        wallet=wallet,
        closed_at=AS_OF + timedelta(days=10),
        entry_price=0.01,
        exit_price=0.99,
        shares_sold=1_000_000.0,
    )

    score = await score_wallet(db_session, wallet, AS_OF, CONFIG)

    assert score.n_positions == 1
    assert score.mean_roi == pytest.approx(Decimal("50") / Decimal("30"))


async def test_position_closed_exactly_at_as_of_excluded(db_session):
    # "Strictly before" per PRD §5.1 — closed_at == as_of must not count.
    wallet = "0xfuture2"
    await add_wallet(db_session, wallet=wallet)
    await add_wcp(db_session, wallet=wallet, closed_at=AS_OF, entry_price=0.5, exit_price=0.9)

    score = await score_wallet(db_session, wallet, AS_OF, CONFIG)

    assert score.n_positions == 0


async def test_future_position_excluded_from_score_universe(db_session):
    wallet = "0xfuture3"
    await add_wallet(db_session, wallet=wallet)
    await add_wcp(
        db_session,
        wallet=wallet,
        closed_at=AS_OF - timedelta(days=5),
        entry_price=0.40,
        exit_price=0.70,
    )
    await add_wcp(
        db_session,
        wallet=wallet,
        closed_at=AS_OF + timedelta(days=5),
        entry_price=0.01,
        exit_price=0.99,
        shares_sold=1_000_000.0,
    )

    scores = await score_universe(db_session, AS_OF, CONFIG, materialize=False)
    by_wallet = {s.wallet: s for s in scores}

    assert by_wallet[wallet].n_positions == 1
