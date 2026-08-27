"""Heuristic event-cluster seeding (architecture §3.2, FR-2).

Precedence (decisions/2026-07-15.md — read that entry for the full
investigation this responds to):

1. `neg_risk_market_id`, when present — Polymarket's own protocol-level
   combinatorial-market group id. Correctly groups categorical multi-outcome
   events (e.g. all ~47 country markets in "who wins the World Cup") that
   share no other identifying field — `event_title` for these is each
   market's own outcome label (e.g. "Argentina"), not a shared title.
2. `(event_title, end_date)`, when neg_risk_market_id is absent —
   `event_title` alone over-clusters: e.g. "Knicks vs. Spurs" recurs across
   many distinct games in the DB (verified: 4 distinct end_date values, ~112-
   119 markets each, one real game per end_date). `end_date` disambiguates.
3. Neither signal, or a group of exactly one market: left unclustered — a
   candidate for `pathfinder clusters review`. Under-clustering is the safe
   failure direction (PRD/architecture principle: conservative by
   construction) — a market that should have been grouped but wasn't just
   gets treated as its own singleton bet, not silently merged with unrelated
   markets.

A market with an existing `source='manual'` event_clusters row is NEVER
touched by heuristic seeding — manual always wins, enforced both in the
Python filtering below AND at the SQL level (ON CONFLICT ... WHERE) as a
race-condition safety net.
"""
from __future__ import annotations

import uuid
from collections import defaultdict
from datetime import datetime, timezone
from typing import Callable

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.db.models.event_cluster import EventCluster
from app.db.models.market import Market
from app.logging import get_logger

logger = get_logger(__name__)

_UPSERT_CHUNK_SIZE = 1000

# Fixed namespaces so cluster_id is deterministic across re-runs — the same
# underlying grouping key always maps to the same cluster_id, so re-seeding
# updates existing heuristic clusters rather than creating duplicates.
_NEG_RISK_NAMESPACE = uuid.UUID("d2f6a1d0-2b1a-4e7d-9c3e-8f1a6b2c3d4e")
_TITLE_DATE_NAMESPACE = uuid.UUID("6b3a9e2f-1c4d-4a8b-9e5f-2d3c4b5a6e7f")


def neg_risk_cluster_id(neg_risk_market_id: str) -> str:
    return f"negrisk:{uuid.uuid5(_NEG_RISK_NAMESPACE, neg_risk_market_id)}"


def title_date_cluster_id(event_title: str, end_date: datetime | None) -> str:
    key = f"{event_title}|{end_date.isoformat() if end_date else ''}"
    return f"title:{uuid.uuid5(_TITLE_DATE_NAMESPACE, key)}"


async def seed_heuristic_clusters(
    session_factory: Callable[[], AsyncSession] | async_sessionmaker,
) -> dict:
    """Group markets into heuristic clusters and upsert into event_clusters.

    session_factory is caller-injected — same dependency-injection shape as
    pathfinder/scoring and pathfinder/booklog (never reach for the app's
    global production session factory internally; see decisions/2026-07-15.md
    for why that matters for testability).
    """
    async with session_factory() as session:
        rows = (
            await session.execute(
                select(Market.market_id, Market.neg_risk_market_id, Market.event_title, Market.end_date)
            )
        ).all()
        manual_market_ids = set(
            (await session.execute(select(EventCluster.market_id).where(EventCluster.source == "manual")))
            .scalars()
            .all()
        )

    neg_risk_groups: dict[str, list[str]] = defaultdict(list)
    title_date_groups: dict[tuple[str, datetime | None], list[str]] = defaultdict(list)
    no_signal = 0

    for market_id, neg_risk_market_id, event_title, end_date in rows:
        if market_id in manual_market_ids:
            continue
        if neg_risk_market_id:
            neg_risk_groups[neg_risk_market_id].append(market_id)
        elif event_title:
            title_date_groups[(event_title, end_date)].append(market_id)
        else:
            no_signal += 1

    assignments: dict[str, str] = {}
    singleton_groups = 0

    for neg_risk_market_id, market_ids in neg_risk_groups.items():
        if len(market_ids) < 2:
            singleton_groups += 1
            continue
        cluster_id = neg_risk_cluster_id(neg_risk_market_id)
        for market_id in market_ids:
            assignments[market_id] = cluster_id

    for (event_title, end_date), market_ids in title_date_groups.items():
        if len(market_ids) < 2:
            singleton_groups += 1
            continue
        cluster_id = title_date_cluster_id(event_title, end_date)
        for market_id in market_ids:
            assignments[market_id] = cluster_id

    if assignments:
        now = datetime.now(tz=timezone.utc)
        rows_to_upsert = [
            {
                "id": str(uuid.uuid4()),
                "cluster_id": cid,
                "market_id": mid,
                "source": "heuristic",
                "updated_at": now,
            }
            for mid, cid in assignments.items()
        ]
        # Postgres caps bind parameters at 65535 per statement (5 params/row
        # here) — chunk the upsert so a large heuristic run (tens of
        # thousands of assignments) doesn't blow past that limit.
        for i in range(0, len(rows_to_upsert), _UPSERT_CHUNK_SIZE):
            chunk = rows_to_upsert[i : i + _UPSERT_CHUNK_SIZE]
            async with session_factory() as session:
                stmt = pg_insert(EventCluster).values(chunk)
                stmt = stmt.on_conflict_do_update(
                    index_elements=["market_id"],
                    set_={
                        "cluster_id": stmt.excluded.cluster_id,
                        "source": stmt.excluded.source,
                        "updated_at": stmt.excluded.updated_at,
                    },
                    where=(EventCluster.source != "manual"),
                )
                await session.execute(stmt)
                await session.commit()

    neg_risk_market_count = sum(1 for cid in assignments.values() if cid.startswith("negrisk:"))
    title_date_market_count = sum(1 for cid in assignments.values() if cid.startswith("title:"))

    result = {
        "total_markets": len(rows),
        "manual_protected": len(manual_market_ids),
        "neg_risk_clusters": len({cid for cid in assignments.values() if cid.startswith("negrisk:")}),
        "neg_risk_market_count": neg_risk_market_count,
        "title_date_clusters": len({cid for cid in assignments.values() if cid.startswith("title:")}),
        "title_date_market_count": title_date_market_count,
        "singleton_groups_left_unclustered": singleton_groups,
        "no_signal_left_unclustered": no_signal,
        "total_clustered": len(assignments),
        "total_unclustered": len(rows) - len(assignments) - len(manual_market_ids),
    }
    logger.info("heuristic_cluster_seed_complete", **result)
    return result
