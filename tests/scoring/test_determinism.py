"""Same DB state + config -> byte-identical output (architecture design
principle #3), for both score_wallet and score_universe, plus idempotent
materialization into wallet_scores_pit.
"""
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import func, select

from pathfinder.config import load_config
from pathfinder.scoring.engine import score_universe, score_wallet
from app.db.models.wallet_score_pit import WalletScorePit
from tests.scoring.conftest import add_wallet, add_wcp

pytestmark = pytest.mark.asyncio

CONFIG = load_config()
AS_OF = datetime(2026, 6, 1, tzinfo=timezone.utc)


async def _seed(db_session, wallet):
    await add_wallet(db_session, wallet=wallet)
    for i in range(20):
        await add_wcp(
            db_session,
            wallet=wallet,
            closed_at=AS_OF - timedelta(days=1, hours=i),
            entry_price=0.30,
            exit_price=0.80,
        )


async def test_score_wallet_is_deterministic(db_session):
    wallet = "0xdeterministic1"
    await _seed(db_session, wallet)

    first = await score_wallet(db_session, wallet, AS_OF, CONFIG)
    second = await score_wallet(db_session, wallet, AS_OF, CONFIG)

    assert first == second


async def test_score_universe_is_deterministic(db_session):
    await _seed(db_session, "0xdeterministic2")
    await _seed(db_session, "0xdeterministic3")

    first = await score_universe(db_session, AS_OF, CONFIG, materialize=False)
    second = await score_universe(db_session, AS_OF, CONFIG, materialize=False)

    assert sorted(first, key=lambda s: s.wallet) == sorted(second, key=lambda s: s.wallet)


async def test_materialize_scores_upsert_is_idempotent(db_session):
    wallet = "0xdeterministic4"
    await _seed(db_session, wallet)

    await score_universe(db_session, AS_OF, CONFIG, materialize=True)
    await score_universe(db_session, AS_OF, CONFIG, materialize=True)

    count = await db_session.execute(
        select(func.count()).select_from(WalletScorePit).where(WalletScorePit.wallet == wallet)
    )
    assert count.scalar_one() == 1
