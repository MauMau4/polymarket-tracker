"""Unit tests for genre-tag-based coverage discovery
(decisions/2026-07-19.md item 4)."""
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.tasks.run_genre_discovery import (
    GENRE_TAG_SLUGS,
    PHASE_1,
    PHASE_2,
    PHASE_3,
    run_genre_discovery,
)


class TestPhaseDeclarations:
    def test_all_phase_genres_are_valid(self):
        for genre in PHASE_1 + PHASE_2 + PHASE_3:
            assert genre in GENRE_TAG_SLUGS

    def test_phases_partition_all_genres_exactly_once(self):
        all_phased = PHASE_1 + PHASE_2 + PHASE_3
        assert sorted(all_phased) == sorted(GENRE_TAG_SLUGS)
        assert len(all_phased) == len(set(all_phased))

    def test_phase_1_is_politics_and_elections(self):
        assert set(PHASE_1) == {"politics", "elections"}


class TestRunGenreDiscoveryGating:
    @pytest.mark.asyncio
    async def test_noop_when_no_genres_enabled(self):
        settings = MagicMock()
        settings.enabled_genre_tags = []
        with patch("app.tasks.run_genre_discovery.get_settings", return_value=settings):
            result = await run_genre_discovery()
        assert result["enabled_genres"] == []
        assert result["total_found"] == 0
        assert result["per_genre"] == {}


class _FakeSessionCM:
    """Minimal async-context-manager stub so `async with factory() as session`
    yields a controllable mock without needing a real DB."""

    def __init__(self, session):
        self._session = session

    async def __aenter__(self):
        return self._session

    async def __aexit__(self, *exc_info):
        return False


class TestGenreDiscoveryDeadlockRecovery:
    """A failed _upsert_market (e.g. a Postgres deadlock against a
    concurrently-running discovery job — market_discovery/sports_discovery/
    genre_discovery all run on independent schedules and can overlap) must
    not poison the rest of the batch. Live-observed regression this session:
    one real deadlock cascaded into 3,800+ duplicate error log lines and a
    whole-job failure (decisions/2026-07-19.md) because the exception
    handler didn't roll back the session before continuing the loop."""

    @pytest.mark.asyncio
    async def test_failed_upsert_rolls_back_and_subsequent_markets_still_process(self):
        settings = MagicMock()
        settings.enabled_genre_tags = ["politics"]

        session = AsyncMock()
        factory = MagicMock(return_value=_FakeSessionCM(session))

        market_ok1 = MagicMock(slug="m1", market_id="1")
        market_bad = MagicMock(slug="m2", market_id="2")
        market_ok2 = MagicMock(slug="m3", market_id="3")
        markets = [market_ok1, market_bad, market_ok2]

        async def fake_upsert_market(sess, market):
            if market.market_id == "2":
                raise RuntimeError("deadlock detected")
            return True

        with patch("app.tasks.run_genre_discovery.get_settings", return_value=settings), \
             patch("app.tasks.run_genre_discovery.get_session_factory", return_value=factory), \
             patch(
                 "app.services.polymarket.gamma_client.fetch_sports_markets_by_tag",
                 new=AsyncMock(return_value=markets),
             ), \
             patch("app.tasks.run_genre_discovery._upsert_market", new=fake_upsert_market), \
             patch("app.tasks.run_genre_discovery._within_discovery_horizon", return_value=True):
            result = await run_genre_discovery()

        assert session.rollback.await_count == 1
        assert result["per_genre"]["politics"]["upserted"] == 2
        assert result["per_genre"]["politics"]["passed_horizon"] == 3
        session.commit.assert_awaited()
