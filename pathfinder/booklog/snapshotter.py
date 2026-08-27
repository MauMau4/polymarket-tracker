"""Book snapshot orchestration (architecture §3.5, FR-5): candidate query ->
batched CLOB fetch -> parse -> write, mirroring the candidate-query/batch-
fetch/per-row-try-except/commit shape of app/tasks/run_price_snapshot.py.
"""
from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timezone
from typing import Callable

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.db.models.book_snapshot import BookSnapshot
from app.db.models.market import Market
from app.db.models.token import Token
from app.logging import get_logger
from pathfinder.booklog.client import best_and_levels, fetch_books_batch
from pathfinder.config import PathfinderConfig

logger = get_logger(__name__)


async def get_candidate_tokens(session: AsyncSession, config: PathfinderConfig) -> list[tuple[str, str]]:
    """One (market_id, asset_id) per active, unresolved market meeting the
    volume floor. Prefers the token labeled 'Yes'; falls back to the
    lowest asset_id for markets without that label (categorical/non-binary),
    so every eligible market is represented by exactly one book — matching
    book_snapshots' one-row-per-market-per-timestamp shape.

    `config.booklog.watchlist_event_titles` bypasses the volume floor by
    exact event_title match (decisions/2026-07-19.md) — live outright
    championship/conference books carry large open interest with modest
    day-to-day volume and are structurally at risk of falling under the
    floor (some already ingested with a stale/null volume field, see that
    entry), yet are exactly the future-work books this booklog needs spread
    history for. Explicit list, not a keyword heuristic — Polymarket's
    event_title naming isn't stable season to season (e.g. the NFL 2024
    season's "AFC Champion"/"NFC Champion" vs. the 2026 season's "Pro
    Football: 2027 AFC/NFC Champion "), so the list needs manual curation
    as each new season's books are discovered/ingested, not something a
    regex can safely auto-extend.
    """
    volume_condition = (
        Market.volume.is_not(None) & (Market.volume >= config.booklog.volume_floor_usd)
    )
    if config.booklog.watchlist_event_titles:
        volume_condition = or_(volume_condition, Market.event_title.in_(config.booklog.watchlist_event_titles))

    result = await session.execute(
        select(Market.market_id, Token.asset_id, Token.outcome)
        .join(Token, Token.market_id == Market.market_id)
        .where(
            Market.active == True,  # noqa: E712
            Market.resolved == False,  # noqa: E712
            volume_condition,
        )
    )

    by_market: dict[str, list[tuple[str, str | None]]] = defaultdict(list)
    for market_id, asset_id, outcome in result.all():
        by_market[market_id].append((asset_id, outcome))

    candidates: list[tuple[str, str]] = []
    for market_id, tokens in by_market.items():
        yes_asset_id = next((a for a, o in tokens if (o or "").strip().lower() == "yes"), None)
        asset_id = yes_asset_id or min(tokens, key=lambda t: t[0])[0]
        candidates.append((market_id, asset_id))
    return candidates


async def take_book_snapshot(
    config: PathfinderConfig,
    session_factory: Callable[[], AsyncSession] | async_sessionmaker,
) -> dict:
    """Snapshot top-of-book for every eligible market once. Returns counts:
    candidates, fetched (had a live book), written, errors.

    session_factory is caller-injected (no internal fallback to the app's
    production get_session_factory()) — same dependency-injection shape as
    pathfinder/scoring/engine.py's score_wallet/score_universe, so this can
    be pointed at a test database without any monkeypatching of DB internals.
    """
    factory = session_factory
    now = datetime.now(tz=timezone.utc)

    async with factory() as session:
        candidates = await get_candidate_tokens(session, config)

    if not candidates:
        logger.info("book_snapshot_no_candidates")
        return {"candidates": 0, "fetched": 0, "written": 0, "errors": 0}

    asset_ids = [asset_id for _, asset_id in candidates]
    books = await fetch_books_batch(asset_ids)

    depth = config.booklog.book_depth_levels
    rows: list[BookSnapshot] = []
    errors = 0

    for market_id, asset_id in candidates:
        raw = books.get(asset_id)
        if raw is None:
            continue
        try:
            best_bid, bid_sz, bid_levels = best_and_levels(raw.get("bids", []), depth, best_is_max=True)
            best_ask, ask_sz, ask_levels = best_and_levels(raw.get("asks", []), depth, best_is_max=False)
            rows.append(
                BookSnapshot(
                    market_id=market_id,
                    asset_id=asset_id,
                    ts=now,
                    best_bid=best_bid,
                    best_ask=best_ask,
                    bid_sz=bid_sz,
                    ask_sz=ask_sz,
                    levels={"bids": bid_levels, "asks": ask_levels},
                )
            )
        except Exception as exc:
            logger.error("book_snapshot_parse_error", market_id=market_id, asset_id=asset_id, error=str(exc))
            errors += 1

    if rows:
        async with factory() as session:
            session.add_all(rows)
            await session.commit()

    result = {
        "candidates": len(candidates),
        "fetched": len(books),
        "written": len(rows),
        "errors": errors,
    }
    logger.info("book_snapshot_complete", **result)
    return result
