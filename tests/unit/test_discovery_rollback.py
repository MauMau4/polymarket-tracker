"""Unit tests for rollback-on-exception in run_discovery/run_sports_discovery
(decisions/2026-07-19.md genre_discovery fix, same pattern applied here).

A failed _upsert_market (e.g. a Postgres deadlock against a concurrently
running discovery job) must not poison the rest of the batch — the exception
handler must roll back the session before continuing the loop, matching
app/tasks/run_genre_discovery.py's fix (tests/unit/test_genre_discovery.py's
TestGenreDiscoveryDeadlockRecovery)."""
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.services.discovery.refresh import run_discovery, run_sports_discovery


class _FakeSessionCM:
    """Minimal async-context-manager stub so `async with factory() as session`
    yields a controllable mock without needing a real DB."""

    def __init__(self, session):
        self._session = session

    async def __aenter__(self):
        return self._session

    async def __aexit__(self, *exc_info):
        return False


class TestRunDiscoveryDeadlockRecovery:
    @pytest.mark.asyncio
    async def test_failed_upsert_rolls_back_and_subsequent_markets_still_process(self):
        session = AsyncMock()
        factory = MagicMock(return_value=_FakeSessionCM(session))

        # category=None keeps each market on the metadata-only path (no
        # token upserts), so the test only exercises the upsert/rollback loop.
        market_ok1 = MagicMock(market_id="1", slug="m1", question="q1", event_title="e1", category=None)
        market_bad = MagicMock(market_id="2", slug="m2", question="q2", event_title="e2", category=None)
        market_ok2 = MagicMock(market_id="3", slug="m3", question="q3", event_title="e3", category=None)
        markets = [market_ok1, market_bad, market_ok2]

        async def fake_upsert_market(sess, market):
            if market.market_id == "2":
                raise RuntimeError("deadlock detected")
            return True

        with patch("app.services.discovery.refresh.fetch_active_markets", new=AsyncMock(return_value=markets)), \
             patch("app.services.discovery.refresh.get_session_factory", return_value=factory), \
             patch("app.services.discovery.refresh._upsert_market", new=fake_upsert_market), \
             patch("app.services.discovery.refresh._within_discovery_horizon", return_value=True):
            result = await run_discovery()

        assert session.rollback.await_count == 1
        assert result.markets_upserted == 2
        session.commit.assert_awaited()


class TestRunSportsDiscoveryDeadlockRecovery:
    @pytest.mark.asyncio
    async def test_failed_upsert_rolls_back_and_subsequent_markets_still_process(self):
        session = AsyncMock()
        factory = MagicMock(return_value=_FakeSessionCM(session))

        market_ok1 = MagicMock(market_id="1", slug="m1", tokens=[])
        market_bad = MagicMock(market_id="2", slug="m2", tokens=[])
        market_ok2 = MagicMock(market_id="3", slug="m3", tokens=[])
        markets = [market_ok1, market_bad, market_ok2]

        async def fake_fetch_sports_markets_by_tag(tag):
            # Only the first tag yields markets; the rest are empty so the
            # rollback count stays deterministic across SPORTS_TAG_SLUGS.
            return markets if tag == "nhl" else []

        async def fake_upsert_market(sess, market):
            if market.market_id == "2":
                raise RuntimeError("deadlock detected")
            return True

        with patch(
            "app.services.polymarket.gamma_client.fetch_sports_markets_by_tag",
            new=AsyncMock(side_effect=fake_fetch_sports_markets_by_tag),
        ), \
             patch("app.services.discovery.refresh.get_session_factory", return_value=factory), \
             patch("app.services.discovery.refresh._upsert_market", new=fake_upsert_market), \
             patch("app.services.discovery.refresh._within_discovery_horizon", return_value=True):
            result = await run_sports_discovery()

        assert session.rollback.await_count == 1
        assert result["per_tag"]["nhl"]["upserted"] == 2
        assert result["per_tag"]["nhl"]["passed_horizon"] == 3
        session.commit.assert_awaited()
