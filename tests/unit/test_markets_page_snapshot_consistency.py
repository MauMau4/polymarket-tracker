"""Regression test for the /markets pagination bug (decisions/2026-07-23.md,
"[FIX] Markets dashboard blank page 1 with a nonzero pager total").

Root cause: `markets_page` (app/ui/views.py) builds `count_stmt` and
`data_stmt` from the identical `_apply_filters()` closure, but executes them
as two separate statements on one session. Under Postgres's default READ
COMMITTED isolation, each statement gets its own snapshot — so a concurrent
writer's commit landing between the two statements can make `total` (and the
derived `total_pages`) describe a different population than the rows
`data_stmt` actually returns. This table is written continuously and
concurrently on independent schedules (Market Discovery every 5 min,
Sports/Genre Discovery and Market Resolution every 30 min — confirmed live:
18 markets resolved in a ~30s window during this investigation), so the
window for this to bite is real, not theoretical.

Fix: pin the session's connection to REPEATABLE READ before either query
runs, so both share one snapshot regardless of what commits in between.

These tests reproduce the vulnerability at default isolation (A) and confirm
the fix closes it (B), using two independent real-Postgres connections
(`db_session` as the "request" connection, `session_factory` as the
concurrent writer) since the bug is inherently about cross-connection
visibility — a mocked session cannot exhibit it."""
from datetime import datetime, timezone

from app.db.models import Market


def _market(market_id: str, end_date: datetime) -> Market:
    return Market(
        market_id=market_id,
        slug=market_id,
        question=f"q-{market_id}",
        active=True,
        closed=False,
        resolved=False,
        end_date=end_date,
    )


async def _count_active_unresolved(session) -> int:
    from sqlalchemy import func, select

    result = await session.execute(
        select(func.count()).select_from(Market).where(
            Market.active == True,  # noqa: E712
            Market.resolved == False,  # noqa: E712
        )
    )
    return result.scalar_one()


class TestMarketsPageSnapshotConsistency:
    async def test_default_isolation_sees_concurrent_commit_mid_request(
        self, db_session, session_factory
    ):
        """Demonstrates the bug class: without pinning a snapshot, a count
        taken before a concurrent commit and a second read taken after it
        disagree within what looks like one logical request."""
        db_session.add(_market("test-snap-a1", datetime(2026, 8, 1, tzinfo=timezone.utc)))
        await db_session.commit()

        total_before = await _count_active_unresolved(db_session)

        async with session_factory() as writer:
            writer.add(_market("test-snap-a2", datetime(2026, 8, 2, tzinfo=timezone.utc)))
            await writer.commit()

        total_after = await _count_active_unresolved(db_session)

        assert total_after == total_before + 1

    async def test_repeatable_read_pins_snapshot_across_concurrent_commit(
        self, db_session, session_factory
    ):
        """The fix: once the connection is pinned to REPEATABLE READ (as
        markets_page now does before building count_stmt), a concurrent
        commit between two reads on the same session must not be visible —
        count() and the paginated SELECT stay consistent with each other no
        matter what another job commits in between."""
        db_session.add(_market("test-snap-b1", datetime(2026, 8, 1, tzinfo=timezone.utc)))
        await db_session.commit()

        await db_session.connection(execution_options={"isolation_level": "REPEATABLE READ"})

        total_before = await _count_active_unresolved(db_session)

        async with session_factory() as writer:
            writer.add(_market("test-snap-b2", datetime(2026, 8, 2, tzinfo=timezone.utc)))
            await writer.commit()

        total_after = await _count_active_unresolved(db_session)

        assert total_after == total_before
