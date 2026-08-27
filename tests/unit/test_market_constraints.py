"""Regression test for the ck_markets_resolved_implies_closed DB constraint
(migrations/versions/0033_markets_resolved_implies_closed.py, decisions/2026-07-23.md
issue 2b).

Root cause this closes: several `run_force_resolve_*` one-off scripts wrote
directly to Market.resolved/resolution via raw ORM attribute assignment,
bypassing `_upsert_market()` (the only code path that previously enforced
`resolved ⇒ closed`) — producing 4 World Cup Winner markets stuck
`resolved=True, closed=False` with a false 'Yes' resolution. A DB constraint
is the right chokepoint specifically because the bug was a *bypass* of the
ORM-level invariant: no future direct-write script, migration, or raw SQL
can slip past a CHECK constraint the way it could slip past `_upsert_market`.

Needs a real Postgres connection (the constraint lives in the schema, not in
application code), hence `db_session` rather than a mock."""
import pytest
from sqlalchemy.exc import IntegrityError

from app.db.models import Market


class TestMarketResolvedImpliesClosedConstraint:
    async def test_resolved_true_closed_false_is_rejected(self, db_session):
        db_session.add(Market(
            market_id="test-invariant-bad",
            slug="test-invariant-bad",
            question="q",
            active=False,
            closed=False,
            resolved=True,
            resolution="Yes",
        ))
        with pytest.raises(IntegrityError):
            await db_session.commit()

    async def test_resolved_true_closed_true_is_allowed(self, db_session):
        db_session.add(Market(
            market_id="test-invariant-good-resolved",
            slug="test-invariant-good-resolved",
            question="q",
            active=False,
            closed=True,
            resolved=True,
            resolution="Yes",
        ))
        await db_session.commit()  # must not raise

    async def test_unresolved_open_market_is_allowed(self, db_session):
        db_session.add(Market(
            market_id="test-invariant-good-open",
            slug="test-invariant-good-open",
            question="q",
            active=True,
            closed=False,
            resolved=False,
        ))
        await db_session.commit()  # must not raise

    async def test_existing_resolved_row_cannot_be_flipped_to_closed_false(self, db_session):
        """An UPDATE, not just an INSERT, must also be caught — this is the exact
        shape of the original bug (an existing row's `resolved` flag flipped
        without `closed` following it)."""
        market = Market(
            market_id="test-invariant-update",
            slug="test-invariant-update",
            question="q",
            active=True,
            closed=False,
            resolved=False,
        )
        db_session.add(market)
        await db_session.commit()

        market.resolved = True
        market.resolution = "Yes"
        with pytest.raises(IntegrityError):
            await db_session.commit()
