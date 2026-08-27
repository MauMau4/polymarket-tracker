"""Genre-tag-based market discovery — politics/elections/economy/tech/science/
weather/pop-culture (decisions/2026-07-18.md coverage-fix proposal,
decisions/2026-07-19.md item 4). Mirrors run_sports_discovery's tag-based
/events/keyset pattern — reuses the same generic fetch_sports_markets_by_tag
function (its name is sports-scoped for historical reasons; the code itself
takes any tag_slug and has no sport-specific logic).

Gated behind settings.enabled_genre_tags (empty by default): a genre is
skipped entirely until its tag is explicitly added there, phase by phase,
after the operator reviews that phase's projection report
(app/tasks/run_genre_discovery_projection.py). Ships inert — deploying this
code changes nothing on its own.

METADATA-ONLY BY DESIGN (operator decision, 2026-07-19 session): this job
upserts Market rows only — it deliberately never inserts Token rows. No
tokens means no WS subscription and no live trade capture (same convention
`run_discovery()` already uses for uncategorizable markets, see
app/services/discovery/refresh.py's `markets_metadata_only` counter). A live
census found ~25k open politics+elections markets with no cap in front of
the WS subscription path (settings.enable_volume_ranked_subscription is off
by default, and in that state the ingestor subscribes to every active/
unresolved market with no limit) — turning that into a live subscription
without sizing it first would have been an uncontrolled ~8-20x jump in
trade-capture load. Metadata-only ingestion gets the real census (market
count, volume distribution) into the DB at zero ingestion-load cost;
subscribing to any of it is a separate, explicitly-approved follow-up step
(app/tasks/run_genre_volume_distribution.py sizes that decision).

Re-runnable/idempotent by design (same upsert machinery as run_discovery /
run_sports_discovery) — safe to run repeatedly to catch markets that were
retagged or missed on a prior pass. Genre tags are declared as lists, not
single strings, so a genre can absorb an additional or renamed tag later
without a code change if Polymarket's tagging drifts — the same class of gap
found for sports in decisions/2026-07-19.md item 1 (superbowl vs nfl).

Micro-lifecycle markets (crypto Up/Down family) are excluded from ingestion
here too, consistent with the existing subscription-side exclusion decision
(decisions/2026-07-18.md) — belt-and-suspenders, since they'd never be
subscribed anyway while everything here stays metadata-only, but exclusion
should hold at every stage per the operator's instruction, not just at
subscription-ranking time.
"""
from __future__ import annotations

from app.config import get_settings
from app.db.session import get_session_factory
from app.logging import get_logger
from app.services.discovery.refresh import (
    _MICRO_LIFECYCLE_SLUG_PATTERN,
    _upsert_market,
    _within_discovery_horizon,
)

logger = get_logger(__name__)

# Genre -> tag_slug(s) to query via Gamma /events/keyset?tag_slug=.
GENRE_TAG_SLUGS: dict[str, list[str]] = {
    "politics": ["politics"],
    "elections": ["elections"],
    "economy": ["economy"],
    "tech": ["tech"],
    "science": ["science"],
    "weather": ["weather"],
    "pop-culture": ["pop-culture"],
}

# Phased activation per decisions/2026-07-19.md item 4 — informational
# grouping only; actual gating is settings.enabled_genre_tags.
PHASE_1 = ["politics", "elections"]
PHASE_2 = ["economy", "tech", "science"]
PHASE_3 = ["weather", "pop-culture"]


async def run_genre_discovery() -> dict:
    """Ingest markets/tokens for every genre in settings.enabled_genre_tags.
    A no-op (returns immediately with empty results) if none are enabled."""
    settings = get_settings()
    enabled = set(settings.enabled_genre_tags)
    factory = get_session_factory()

    per_genre: dict[str, dict] = {}
    total_found = total_upserted = total_excluded_micro = 0

    if not enabled:
        logger.debug("genre_discovery_skipped_none_enabled")
        return {
            "enabled_genres": [],
            "total_found": 0,
            "total_upserted": 0,
            "total_excluded_micro_lifecycle": 0,
            "per_genre": {},
        }

    from app.services.polymarket.gamma_client import fetch_sports_markets_by_tag

    for genre, tag_slugs in GENRE_TAG_SLUGS.items():
        if genre not in enabled:
            continue

        found = upserted = excluded_micro = passed_horizon = 0

        for tag_slug in tag_slugs:
            try:
                markets = await fetch_sports_markets_by_tag(tag_slug)
            except Exception as exc:
                logger.error(
                    "genre_discovery_tag_fetch_error", genre=genre, tag_slug=tag_slug, error=str(exc)
                )
                continue

            found += len(markets)

            async with factory() as session:
                for market in markets:
                    if _MICRO_LIFECYCLE_SLUG_PATTERN.search(market.slug or ""):
                        excluded_micro += 1
                        continue
                    if not _within_discovery_horizon(market):
                        continue
                    passed_horizon += 1
                    try:
                        # Metadata-only: Market row only, no Token rows — see
                        # module docstring. Never call _upsert_token here.
                        is_new = await _upsert_market(session, market)
                        if is_new:
                            upserted += 1
                    except Exception as exc:
                        logger.error(
                            "genre_discovery_upsert_error",
                            genre=genre,
                            market_id=market.market_id,
                            error=str(exc),
                        )
                        # A failed flush (e.g. a Postgres deadlock against a
                        # concurrent discovery job writing the same market
                        # row — market_discovery/sports_discovery/this job
                        # all run on independent schedules and can overlap)
                        # leaves the session's transaction unusable until
                        # rolled back. Without this, every remaining market
                        # in this batch fails identically (observed live:
                        # one real deadlock cascaded into 3,800+ duplicate
                        # error log lines and a whole-job failure,
                        # decisions/2026-07-19.md). Idempotent either way —
                        # anything lost here is just re-upserted next run.
                        await session.rollback()
                await session.commit()

        per_genre[genre] = {
            "found": found,
            "passed_horizon": passed_horizon,
            "upserted": upserted,
            "excluded_micro_lifecycle": excluded_micro,
        }
        total_found += found
        total_upserted += upserted
        total_excluded_micro += excluded_micro

        logger.info("genre_discovery_tag_complete", genre=genre, **per_genre[genre])

    result = {
        "enabled_genres": sorted(enabled),
        "total_found": total_found,
        "total_upserted": total_upserted,
        "total_excluded_micro_lifecycle": total_excluded_micro,
        "per_genre": per_genre,
    }
    logger.info(
        "genre_discovery_complete",
        enabled_genres=result["enabled_genres"],
        total_found=total_found,
        total_upserted=total_upserted,
        total_excluded_micro_lifecycle=total_excluded_micro,
    )
    return result
