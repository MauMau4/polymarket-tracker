"""CLI for Pathfinder event-cluster tooling (architecture §3.2, FR-2).

Invocation: `python -m pathfinder.clusters.cli <review|assign|seed> ...`

Architecture describes this as `pathfinder clusters review` / `pathfinder
clusters assign <market> <cluster>` — that literal syntax implies a
registered console-script entry point, which no part of this repo uses
anywhere (every existing task is `python -m app.tasks.xxx`, argparse-based,
no click/typer dependency). This follows the repo's existing convention
instead of introducing a new CLI framework for one small feature — logged
in decisions/2026-07-15.md. Functionally equivalent commands, different
invocation string.
"""
from __future__ import annotations

import argparse
import asyncio
import sys
import uuid
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert

from app.db.models.event_cluster import EventCluster
from app.db.models.market import Market
from app.db.session import get_session_factory
from app.logging import setup_logging
from pathfinder.clusters.heuristics import seed_heuristic_clusters


async def review(limit: int, show_all: bool) -> None:
    """List markets with no event_clusters row at all (manual or heuristic)."""
    factory = get_session_factory()
    async with factory() as session:
        stmt = (
            select(Market.market_id, Market.question, Market.event_title, Market.volume)
            .outerjoin(EventCluster, EventCluster.market_id == Market.market_id)
            .where(EventCluster.id.is_(None))
        )
        if not show_all:
            stmt = stmt.where(Market.active == True, Market.resolved == False)  # noqa: E712
        stmt = stmt.order_by(Market.volume.desc().nulls_last()).limit(limit)
        rows = (await session.execute(stmt)).all()

    if not rows:
        print("No unclustered markets found.")
        return

    print(f"{'market_id':<12} {'volume':>14}  {'event_title':<40} question")
    for market_id, question, event_title, volume in rows:
        vol_str = f"{volume:,.0f}" if volume is not None else "—"
        title = (event_title or "—")[:40]
        print(f"{market_id:<12} {vol_str:>14}  {title:<40} {question or ''}")


async def assign(market_id: str, cluster_id: str) -> None:
    """Manually assign a market to a cluster. Always wins over the heuristic."""
    factory = get_session_factory()
    now = datetime.now(tz=timezone.utc)
    async with factory() as session:
        exists = (
            await session.execute(select(Market.market_id).where(Market.market_id == market_id))
        ).scalar_one_or_none()
        if exists is None:
            print(f"error: market_id {market_id!r} not found in markets table")
            return

        stmt = pg_insert(EventCluster).values(
            id=str(uuid.uuid4()),
            cluster_id=cluster_id,
            market_id=market_id,
            source="manual",
            updated_at=now,
        )
        stmt = stmt.on_conflict_do_update(
            index_elements=["market_id"],
            set_={"cluster_id": cluster_id, "source": "manual", "updated_at": now},
        )
        await session.execute(stmt)
        await session.commit()

    print(f"assigned {market_id} -> cluster {cluster_id} (source=manual)")


async def seed() -> None:
    setup_logging()
    result = await seed_heuristic_clusters(get_session_factory())
    print("=== Heuristic cluster seed ===")
    for key, value in result.items():
        print(f"  {key}: {value}")


async def _dispatch(args: argparse.Namespace) -> None:
    if args.command == "review":
        await review(args.limit, args.show_all)
    elif args.command == "assign":
        await assign(args.market_id, args.cluster_id)
    elif args.command == "seed":
        await seed()


def main() -> None:
    parser = argparse.ArgumentParser(prog="pathfinder-clusters")
    sub = parser.add_subparsers(dest="command", required=True)

    p_review = sub.add_parser("review", help="List markets not in any cluster")
    p_review.add_argument("--limit", type=int, default=50)
    p_review.add_argument("--all", action="store_true", dest="show_all", help="Include resolved/inactive markets")

    p_assign = sub.add_parser("assign", help="Manually assign a market to a cluster")
    p_assign.add_argument("market_id")
    p_assign.add_argument("cluster_id")

    sub.add_parser("seed", help="Run heuristic cluster seeding against the full markets table")

    args = parser.parse_args()

    if sys.platform == "win32":
        asyncio.run(_dispatch(args), loop_factory=asyncio.SelectorEventLoop)
    else:
        asyncio.run(_dispatch(args))


if __name__ == "__main__":
    main()
