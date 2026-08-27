"""Promotes the top-N-by-volume markets for specified genres from
metadata-only to fully subscribable, by adding Token rows
(decisions/2026-07-19.md, approved subscription-expansion step following
the volume-distribution sizing report).

Deliberately does NOT touch settings.enable_volume_ranked_subscription or
subscription_top_n — the existing default (uncapped) WS-subscription path
in run_ws_ingestor.py already subscribes to every market with Token rows, so
adding tokens for exactly the top-N selected markets is sufficient on its
own and has no effect on anything else's subscription. Selection itself
uses the same volume-ranking logic as run_genre_volume_distribution.py (live
Gamma census, sorted by lifetime volume descending, micro-lifecycle
excluded) — "via the volume-ranked mechanism" in the sense of *selection*,
not by flipping the global volume-ranked-subscription cap.

Idempotent/re-runnable: safe to call again with a larger N later (adds only
the newly-promoted markets' tokens; already-promoted ones are unchanged).

Run: python -m app.tasks.run_genre_subscription_expansion politics elections --top-n 30
"""
from __future__ import annotations

import asyncio
import sys
from datetime import datetime, timezone

from app.db.session import get_session_factory
from app.logging import get_logger, setup_logging
from app.services.discovery.refresh import _MICRO_LIFECYCLE_SLUG_PATTERN, _upsert_market, _upsert_token
from app.services.polymarket.gamma_client import fetch_sports_markets_by_tag
from app.tasks.run_genre_discovery import GENRE_TAG_SLUGS

logger = get_logger(__name__)


async def _collect_genre_markets(genres: list[str]):
    """Live census of full MarketInfo objects (with .tokens populated) for
    the given genres, deduped across overlapping tags, micro-lifecycle
    excluded."""
    seen = {}
    for genre in genres:
        for tag_slug in GENRE_TAG_SLUGS[genre]:
            markets = await fetch_sports_markets_by_tag(tag_slug)
            for m in markets:
                if _MICRO_LIFECYCLE_SLUG_PATTERN.search(m.slug or ""):
                    continue
                seen[m.market_id] = m
    return sorted(seen.values(), key=lambda m: -(m.volume or 0.0))


async def expand_genre_subscription(genres: list[str], top_n: int) -> dict:
    ranked = await _collect_genre_markets(genres)
    top = ranked[:top_n]

    factory = get_session_factory()
    promoted_markets = 0
    promoted_tokens = 0
    market_ids: list[str] = []

    async with factory() as session:
        for market in top:
            await _upsert_market(session, market)  # idempotent refresh, no horizon gate needed (already watchlisted-equivalent by explicit N selection)
            any_new_token = False
            for token in market.tokens:
                if not token.token_id:
                    continue
                is_new = await _upsert_token(session, market.market_id, token.token_id, token.outcome)
                if is_new:
                    promoted_tokens += 1
                    any_new_token = True
            if any_new_token:
                promoted_markets += 1
            market_ids.append(market.market_id)
        await session.commit()

    result = {
        "genres": genres,
        "top_n": top_n,
        "universe_size": len(ranked),
        "promoted_markets": promoted_markets,
        "promoted_tokens": promoted_tokens,
        "market_ids": market_ids,
        "min_volume_in_selection": top[-1].volume if top else None,
        "max_volume_in_selection": top[0].volume if top else None,
    }
    logger.info(
        "genre_subscription_expansion_complete",
        genres=genres,
        top_n=top_n,
        promoted_markets=promoted_markets,
        promoted_tokens=promoted_tokens,
    )
    return result


async def main() -> None:
    setup_logging()
    args = sys.argv[1:]
    top_n = 30
    if "--top-n" in args:
        idx = args.index("--top-n")
        top_n = int(args[idx + 1])
        args = args[:idx] + args[idx + 2:]
    genres = args or ["politics", "elections"]

    print(f"=== Genre Subscription Expansion — {datetime.now(tz=timezone.utc).isoformat()} ===")
    print(f"Genres: {genres}, top_n={top_n}\n")

    result = await expand_genre_subscription(genres, top_n)
    print(f"Universe size            : {result['universe_size']}")
    print(f"Promoted markets (new)   : {result['promoted_markets']}")
    print(f"Promoted tokens (new)    : {result['promoted_tokens']}")
    print(f"Volume range in selection: ${result['min_volume_in_selection']:,.0f} - ${result['max_volume_in_selection']:,.0f}")
    print(f"Market IDs: {result['market_ids']}")


if __name__ == "__main__":
    if sys.platform == "win32":
        asyncio.run(main(), loop_factory=asyncio.SelectorEventLoop)
    else:
        asyncio.run(main())
