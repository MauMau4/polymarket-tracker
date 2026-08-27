"""Regression tests for the outcome_result propagation fix
(app/services/markets/resolution.py, migrations/versions/0034,
decisions/2026-08-06.md, punch list item 0).

Root cause this closes: trades.outcome_result was stamped once, at first
resolution, gated on `outcome_result IS NULL`. A later correction to
markets.resolution (the 07-23 World Cup false-Yes fix, or an unidentified
earlier correction on 5 other markets) left already-stamped trades frozen
inverted — because they were no longer NULL, the old
`_find_markets_needing_scoring()` never saw them again. Measured: 15,189
trades across 9 markets, $5,607,098 staked (scripts/verify_worldcup_outcome_result.sql).

test_correction_after_stamp_propagates_to_trades is written to demonstrably
fail against the pre-fix implementation: the old gate
(`Trade.outcome_result.is_(None)`) would never re-select this market once its
trades already carried a value, so the fixed assertions here would fail with
stale outcome_result on the old code path.

Needs a real Postgres connection (the fix relies on a DB column,
outcome_result_stamped_resolution, and an IS DISTINCT FROM comparison), hence
`db_session` rather than a mock.
"""
import pytest
from sqlalchemy import select

from app.db.models.market import Market
from app.db.models.token import Token
from app.db.models.trade import Trade
from app.services.markets.resolution import (
    _find_markets_needing_restamp,
    _process_market_resolution,
    _restamp_trade_outcome_results,
)


async def _seed_two_outcome_market(db_session, market_id: str, resolution: str, stamped_resolution: str):
    db_session.add(Market(
        market_id=market_id,
        slug=market_id,
        question="q",
        active=False,
        closed=True,
        resolved=True,
        resolution=resolution,
        outcome_result_stamped_resolution=stamped_resolution,
    ))
    db_session.add(Token(asset_id=f"{market_id}-yes", market_id=market_id, outcome="Yes"))
    db_session.add(Token(asset_id=f"{market_id}-no", market_id=market_id, outcome="No"))
    await db_session.flush()


def _buy_trade(market_id: str, asset_id: str, outcome: str, wallet: str, outcome_result: int | None):
    return Trade(
        ts=__import__("datetime").datetime.now(tz=__import__("datetime").timezone.utc),
        market_id=market_id,
        asset_id=asset_id,
        outcome=outcome,
        price=0.5,
        size=10.0,
        side="BUY",
        wallet=wallet,
        source="test",
        outcome_result=outcome_result,
    )


class TestResolutionCorrectionPropagates:
    async def test_correction_after_stamp_propagates_to_trades(self, db_session):
        """The exact shape of the original bug: trades stamped against a
        resolution that markets.resolution no longer holds. Fails against the
        pre-fix `outcome_result IS NULL` gate, which would never re-select
        this market once outcome_result was non-NULL."""
        market_id = "test-restamp-correction"
        # Simulate: market was originally (wrongly) resolved "Yes" and its
        # trades were stamped against that — outcome_result_stamped_resolution
        # records what the stamp was actually computed against.
        await _seed_two_outcome_market(db_session, market_id, resolution="Yes", stamped_resolution="Yes")
        db_session.add(_buy_trade(market_id, f"{market_id}-yes", "Yes", "0xwallet1", outcome_result=1))
        db_session.add(_buy_trade(market_id, f"{market_id}-no", "No", "0xwallet2", outcome_result=0))
        await db_session.commit()

        # Operator corrects the resolution (mirrors the 07-23 World Cup fix).
        market = (await db_session.execute(
            select(Market).where(Market.market_id == market_id)
        )).scalar_one()
        market.resolution = "No"
        await db_session.commit()

        candidates = await _find_markets_needing_restamp(db_session)
        assert market_id in candidates, (
            "market whose resolution changed after trades were stamped must "
            "be a re-stamp candidate"
        )

        await _process_market_resolution(db_session, market_id)
        await db_session.commit()

        trades = (await db_session.execute(
            select(Trade)
            .where(Trade.market_id == market_id)
            .order_by(Trade.wallet)
            .execution_options(populate_existing=True)
        )).scalars().all()
        by_wallet = {t.wallet: t for t in trades}
        assert by_wallet["0xwallet1"].outcome_result == 0, "Yes-buyer must flip to a loss under resolution=No"
        assert by_wallet["0xwallet2"].outcome_result == 1, "No-buyer must flip to a win under resolution=No"

        market = (await db_session.execute(
            select(Market).where(Market.market_id == market_id)
        )).scalar_one()
        assert market.outcome_result_stamped_resolution == "No"

        # Idempotent: re-running finds nothing left to do.
        assert await _find_markets_needing_restamp(db_session) == []

    async def test_late_null_trade_is_still_a_candidate_even_if_stamp_already_matches(self, db_session):
        """The migration-orphaning bug found 2026-08-06: a market whose
        outcome_result_stamped_resolution already agrees with resolution (so
        trigger B is silent) but that has a genuinely NULL, never-stamped BUY
        trade (e.g. arrived late) must still surface via trigger A. Confirmed
        live: migration 0034's blanket backfill made this true for 295
        markets before this OR was added back."""
        market_id = "test-restamp-late-null-trade"
        await _seed_two_outcome_market(db_session, market_id, resolution="No", stamped_resolution="No")
        db_session.add(_buy_trade(market_id, f"{market_id}-no", "No", "0xwallet1", outcome_result=1))
        db_session.add(_buy_trade(market_id, f"{market_id}-no", "No", "0xwallet2", outcome_result=None))
        await db_session.commit()

        assert market_id in await _find_markets_needing_restamp(db_session)

    async def test_unchanged_resolution_is_not_a_restamp_candidate(self, db_session):
        """Steady state: a market whose stamp already agrees with its
        resolution must not be re-processed every cycle."""
        market_id = "test-restamp-steady-state"
        await _seed_two_outcome_market(db_session, market_id, resolution="No", stamped_resolution="No")
        db_session.add(_buy_trade(market_id, f"{market_id}-no", "No", "0xwallet1", outcome_result=1))
        await db_session.commit()

        assert market_id not in await _find_markets_needing_restamp(db_session)

    async def test_first_time_resolution_is_a_restamp_candidate(self, db_session):
        """outcome_result_stamped_resolution NULL (never stamped) must still
        trigger — the new trigger generalizes, not replaces, the old
        first-stamp case."""
        market_id = "test-restamp-first-time"
        await _seed_two_outcome_market(db_session, market_id, resolution="Yes", stamped_resolution=None)
        db_session.add(_buy_trade(market_id, f"{market_id}-yes", "Yes", "0xwallet1", outcome_result=None))
        await db_session.commit()

        assert market_id in await _find_markets_needing_restamp(db_session)


class TestRestampNeverFabricates:
    async def test_recoverable_null_outcome_is_backfilled_then_stamped(self, db_session):
        market_id = "test-restamp-null-recoverable"
        await _seed_two_outcome_market(db_session, market_id, resolution="No", stamped_resolution=None)
        trade = _buy_trade(market_id, f"{market_id}-no", None, "0xwallet1", outcome_result=None)
        db_session.add(trade)
        await db_session.commit()

        await _restamp_trade_outcome_results(db_session, market_id, "NO")
        await db_session.commit()

        refreshed = (await db_session.execute(
            select(Trade).where(Trade.id == trade.id).execution_options(populate_existing=True)
        )).scalar_one()
        assert refreshed.outcome == "No", "outcome should be backfilled from tokens.outcome via asset_id"
        assert refreshed.outcome_result == 1

    async def test_unrecoverable_null_outcome_stays_null_not_fabricated_zero(self, db_session):
        """decisions/2026-08-06.md Amendment 1: the old bulk update's
        coalesce(outcome,'') fabricated a 0 for any trade whose outcome (and
        token.outcome) is unrecoverably NULL. Must stay NULL instead."""
        market_id = "test-restamp-null-unrecoverable"
        db_session.add(Market(
            market_id=market_id, slug=market_id, question="q",
            active=False, closed=True, resolved=True, resolution="No",
        ))
        # No Token rows at all — outcome is unrecoverable.
        trade = _buy_trade(market_id, f"{market_id}-orphan-asset", None, "0xwallet1", outcome_result=None)
        db_session.add(trade)
        await db_session.commit()

        await _restamp_trade_outcome_results(db_session, market_id, "NO")
        await db_session.commit()

        refreshed = (await db_session.execute(
            select(Trade).where(Trade.id == trade.id).execution_options(populate_existing=True)
        )).scalar_one()
        assert refreshed.outcome is None
        assert refreshed.outcome_result is None, "must not fabricate a 0 for an unrecoverable outcome"
