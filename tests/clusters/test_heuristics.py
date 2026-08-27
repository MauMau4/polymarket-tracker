"""pathfinder/clusters/heuristics.py — precedence rules verified against the
real failure modes found in production data (decisions/2026-07-15.md):
  - "Knicks vs. Spurs": one event_title, 4 distinct real games (end_date) —
    must NOT merge into one cluster.
  - "World Cup Winner": ~47 country markets, each with its OWN event_title
    (its country name) but a SHARED neg_risk_market_id — must merge into one
    cluster despite having no common event_title.
"""
from datetime import datetime, timezone

import pytest
from sqlalchemy import select

from app.db.models.event_cluster import EventCluster
from pathfinder.clusters.heuristics import seed_heuristic_clusters
from tests.booklog.conftest import add_market

pytestmark = pytest.mark.asyncio

GAME_1 = datetime(2026, 6, 4, 0, 30, tzinfo=timezone.utc)
GAME_2 = datetime(2026, 6, 6, 0, 30, tzinfo=timezone.utc)


async def _clusters_by_market(session) -> dict[str, EventCluster]:
    rows = (await session.execute(select(EventCluster))).scalars().all()
    return {row.market_id: row for row in rows}


# ---------------------------------------------------------------------------
# neg_risk_market_id precedence — the "World Cup Winner" case
# ---------------------------------------------------------------------------
async def test_neg_risk_group_clusters_despite_distinct_event_titles(db_session, session_factory):
    # Mirrors the real Argentina/Brazil/Germany case: each has its own
    # event_title (its country name), no shared title, but one negRiskMarketID.
    await add_market(db_session, market_id="m_arg", volume=100, event_title="Argentina", neg_risk_market_id="negrisk-wc")
    await add_market(db_session, market_id="m_bra", volume=100, event_title="Brazil", neg_risk_market_id="negrisk-wc")
    await add_market(db_session, market_id="m_ger", volume=100, event_title="Germany", neg_risk_market_id="negrisk-wc")
    await db_session.commit()

    result = await seed_heuristic_clusters(session_factory)

    assert result["neg_risk_market_count"] == 3
    assert result["neg_risk_clusters"] == 1

    clusters = await _clusters_by_market(db_session)
    assert clusters["m_arg"].cluster_id == clusters["m_bra"].cluster_id == clusters["m_ger"].cluster_id
    assert clusters["m_arg"].source == "heuristic"


async def test_neg_risk_singleton_left_unclustered(db_session, session_factory):
    await add_market(db_session, market_id="m_solo", volume=100, neg_risk_market_id="negrisk-lonely")
    await db_session.commit()

    result = await seed_heuristic_clusters(session_factory)

    assert result["neg_risk_market_count"] == 0
    clusters = await _clusters_by_market(db_session)
    assert "m_solo" not in clusters


# ---------------------------------------------------------------------------
# (event_title, end_date) fallback — the "Knicks vs. Spurs" case
# ---------------------------------------------------------------------------
async def test_same_title_different_end_date_not_merged(db_session, session_factory):
    # Same real bug this heuristic exists to avoid: without end_date, all 4
    # would merge into one 353-market cluster spanning unrelated games.
    await add_market(db_session, market_id="m_g1_spread", volume=100, event_title="Knicks vs. Spurs", end_date=GAME_1)
    await add_market(db_session, market_id="m_g1_ou", volume=100, event_title="Knicks vs. Spurs", end_date=GAME_1)
    await add_market(db_session, market_id="m_g2_spread", volume=100, event_title="Knicks vs. Spurs", end_date=GAME_2)
    await add_market(db_session, market_id="m_g2_ou", volume=100, event_title="Knicks vs. Spurs", end_date=GAME_2)
    await db_session.commit()

    result = await seed_heuristic_clusters(session_factory)

    assert result["title_date_clusters"] == 2  # two separate games, not one
    clusters = await _clusters_by_market(db_session)
    assert clusters["m_g1_spread"].cluster_id == clusters["m_g1_ou"].cluster_id
    assert clusters["m_g2_spread"].cluster_id == clusters["m_g2_ou"].cluster_id
    assert clusters["m_g1_spread"].cluster_id != clusters["m_g2_spread"].cluster_id


async def test_title_date_singleton_left_unclustered(db_session, session_factory):
    await add_market(db_session, market_id="m_alone", volume=100, event_title="Rare Game", end_date=GAME_1)
    await db_session.commit()

    result = await seed_heuristic_clusters(session_factory)

    assert result["title_date_market_count"] == 0
    clusters = await _clusters_by_market(db_session)
    assert "m_alone" not in clusters


async def test_no_event_title_no_neg_risk_left_unclustered(db_session, session_factory):
    await add_market(db_session, market_id="m_nothing", volume=100)
    await db_session.commit()

    result = await seed_heuristic_clusters(session_factory)

    assert result["no_signal_left_unclustered"] == 1
    clusters = await _clusters_by_market(db_session)
    assert "m_nothing" not in clusters


# ---------------------------------------------------------------------------
# Manual assignments always win
# ---------------------------------------------------------------------------
async def test_manual_assignment_never_overwritten_by_heuristic(db_session, session_factory):
    await add_market(db_session, market_id="m_a", volume=100, neg_risk_market_id="negrisk-x")
    await add_market(db_session, market_id="m_b", volume=100, neg_risk_market_id="negrisk-x")
    await db_session.commit()

    # Operator manually assigns m_a to a different, hand-picked cluster BEFORE seeding.
    async with session_factory() as session:
        session.add(EventCluster(id="manual-1", cluster_id="operator-chosen", market_id="m_a", source="manual"))
        await session.commit()

    result = await seed_heuristic_clusters(session_factory)

    assert result["manual_protected"] == 1
    clusters = await _clusters_by_market(db_session)
    assert clusters["m_a"].source == "manual"
    assert clusters["m_a"].cluster_id == "operator-chosen"  # untouched
    # m_b has no cluster partner left (its only neg_risk sibling is manually
    # claimed elsewhere) — group size 1 among non-manual markets, unclustered.
    assert "m_b" not in clusters


# ---------------------------------------------------------------------------
# Idempotent re-seeding
# ---------------------------------------------------------------------------
async def test_reseeding_is_idempotent(db_session, session_factory):
    await add_market(db_session, market_id="m_x", volume=100, neg_risk_market_id="negrisk-y")
    await add_market(db_session, market_id="m_z", volume=100, neg_risk_market_id="negrisk-y")
    await db_session.commit()

    first = await seed_heuristic_clusters(session_factory)
    clusters_first = await _clusters_by_market(db_session)

    second = await seed_heuristic_clusters(session_factory)
    clusters_second = await _clusters_by_market(db_session)

    assert first["neg_risk_clusters"] == second["neg_risk_clusters"] == 1
    assert clusters_first["m_x"].cluster_id == clusters_second["m_x"].cluster_id


# ---------------------------------------------------------------------------
# Postgres bind-parameter limit (65535) — hit for real against production
# with ~52k markets / 5 params per row; regression-tested with a group large
# enough to force multiple upsert chunks (_UPSERT_CHUNK_SIZE = 1000).
# ---------------------------------------------------------------------------
async def test_large_cluster_exceeding_upsert_chunk_size(db_session, session_factory):
    n = 1500
    for i in range(n):
        await add_market(db_session, market_id=f"m_big_{i}", volume=100, neg_risk_market_id="negrisk-big")
    await db_session.commit()

    result = await seed_heuristic_clusters(session_factory)

    assert result["neg_risk_market_count"] == n
    clusters = await _clusters_by_market(db_session)
    assert len(clusters) == n
    assert len({c.cluster_id for c in clusters.values()}) == 1
