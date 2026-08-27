"""Market discovery: fetch active markets from Gamma and upsert to DB."""
import re
from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Market, Token, Wallet, WalletPosition
from app.db.session import get_session_factory
from app.logging import get_logger
from app.schemas.market import MarketDiscoveryResult, MarketInfo
from app.services.polymarket.gamma_client import fetch_active_markets

# Crypto "Up or Down" and equivalent 5-15 minute micro-lifecycle markets
# (e.g. btc-updown-5m-1784334300) — structurally unfollowable by every
# strategy variant in this codebase and continuously spawning (200-500+/day),
# see decisions/2026-07-17.md and 2026-07-18.md. Matched by slug pattern
# since these markets carry no duration field of their own in `markets`.
_MICRO_LIFECYCLE_SLUG_PATTERN = re.compile(r"-updown-\d+m-", re.IGNORECASE)

_SPORTS_SUBCAT_PREFIXES: list[tuple[list[str], str]] = [
    (["nba", "basketball"], "Basketball"),
    (["nfl", "super bowl", "nfl draft"], "American Football"),
    (["ufc", "mma", "boxing"], "Combat Sports"),
    (["mlb", "baseball"], "Baseball"),
    (["nhl", "hockey", "stanley cup"], "Hockey"),
    (["epl", "premier league", "champions league", "mls", "fifa",
      "soccer", "la liga", "serie a", "ligue 1", "bundesliga",
      "copa america", "ballon dor"], "Soccer"),
    (["tennis", "wimbledon", "grand slam", "atp", "wta"], "Tennis"),
    (["f1", "formula", "nascar", "grand prix"], "Motorsports"),
    (["pga", "golf", "masters"], "Golf"),
    (["ncaa", "cfb", "college"], "College Sports"),
    (["cricket", "ipl"], "Cricket"),
    (["rugby", "six nations"], "Rugby"),
]

_CRYPTO_SUBCAT_PREFIXES: list[tuple[list[str], str]] = [
    (["bitcoin", "btc"], "Bitcoin"),
    (["ethereum", "eth"], "Ethereum"),
    (["solana", "sol"], "Solana"),
    (["xrp", "ripple"], "XRP"),
    (["doge", "dogecoin"], "Dogecoin"),
    (["bnb", "binance"], "BNB"),
    (["avax", "avalanche"], "Avalanche"),
    (["ada", "cardano"], "Cardano"),
]


def _infer_subcategory(slug: str | None, question: str | None, event_title: str | None, category: str | None) -> str | None:
    if not category:
        return None
    combined = " ".join(filter(None, [slug, question, event_title])).lower().replace("-", " ")
    if category == "Sports":
        prefixes = _SPORTS_SUBCAT_PREFIXES
    elif category == "Crypto":
        prefixes = _CRYPTO_SUBCAT_PREFIXES
    else:
        return None
    for keywords, subcat in prefixes:
        if any(kw in combined for kw in keywords):
            return subcat
    return None

logger = get_logger(__name__)


async def _upsert_market(session: AsyncSession, info: MarketInfo) -> bool:
    """Insert or update a market record. Returns True if it was new."""
    result = await session.execute(select(Market).where(Market.market_id == info.market_id))
    existing = result.scalar_one_or_none()

    if existing is None:
        category = info.category
        session.add(Market(
            market_id=info.market_id,
            condition_id=info.condition_id,
            slug=info.slug,
            question=info.question,
            event_title=info.event_title,
            category=category,
            subcategory=_infer_subcategory(info.slug, info.question, info.event_title, category),
            active=False if info.resolved else info.active,
            closed=True if info.resolved else info.closed,
            resolved=info.resolved,
            resolution=info.resolution,
            end_date=info.end_date,
            volume=info.volume,
            neg_risk_market_id=info.neg_risk_market_id,
        ))
        return True
    else:
        # Never regress resolution state: discovery only sees Gamma's active
        # list, so an already-resolved local market must stay resolved.
        if info.resolved and not existing.resolved:
            existing.resolved = True
        if info.resolution and not existing.resolution:
            existing.resolution = info.resolution
        is_resolved = existing.resolved or info.resolved
        existing.active = False if is_resolved else info.active
        existing.closed = True if is_resolved else info.closed
        existing.end_date = info.end_date
        if info.volume is not None:
            existing.volume = info.volume
        if info.neg_risk_market_id and not existing.neg_risk_market_id:
            existing.neg_risk_market_id = info.neg_risk_market_id
        if info.question:
            existing.question = info.question
        if info.event_title:
            existing.event_title = info.event_title
        if info.category and not existing.category:
            existing.category = info.category
            existing.subcategory = _infer_subcategory(
                info.slug, info.question, info.event_title, info.category
            )
        existing.updated_at = datetime.now(tz=timezone.utc)
        return False


async def _upsert_token(session: AsyncSession, market_id: str, token_id: str, outcome: str | None) -> bool:
    result = await session.execute(select(Token).where(Token.asset_id == token_id))
    existing = result.scalar_one_or_none()
    if existing is None:
        session.add(Token(
            asset_id=token_id,
            market_id=market_id,
            outcome=outcome,
            token_id=token_id,
        ))
        return True
    # Update outcome if Gamma returns a non-null value that differs from what's stored.
    # This corrects stale labels (e.g. "Down" → "No") that arrived from an earlier
    # discovery pass or a bad Data API response.
    if outcome is not None and existing.outcome != outcome:
        existing.outcome = outcome
    return False


_DISCOVERY_END_DATE_HORIZON_DAYS = 30


def _is_watchlisted(info: MarketInfo) -> bool:
    """True if this market's event is on pathfinder's booklog watchlist
    (config/pathfinder.yaml booklog.watchlist_event_titles) — live outright
    championship/conference books whose single shared event end_date sits
    far beyond the ordinary 30-day discovery horizon (season/tournament
    finals, not the book's own creation date). Same bypass mechanism as the
    booklog volume-floor bypass (pathfinder/booklog/snapshotter.py), applied
    here to discovery itself (decisions/2026-07-19.md item 2): those books
    were found completely or mostly un-ingested (e.g. AFC Champion 2026:
    0/17 markets) because every sibling market in the event shares that same
    distant end_date, so the horizon filter below was blocking the entire
    event, not just late entrants. Never used to admit non-watchlisted
    markets — this is an explicit, hand-curated title list, not a heuristic.
    """
    if not info.event_title:
        return False
    try:
        from pathfinder.config import get_config
        watchlist = get_config().booklog.watchlist_event_titles
    except Exception:
        return False
    return info.event_title in watchlist


def _within_discovery_horizon(info: MarketInfo) -> bool:
    """Return True if this market should be written to the DB.

    Resolved markets are always kept (they feed scoring history).
    Markets with no end_date are kept (cannot determine closeness).
    Watchlisted markets (see _is_watchlisted) are always kept regardless of
    end_date.
    Active markets are filtered to those closing within 30 days.
    """
    if info.resolved:
        return True
    if info.end_date is None:
        return True
    if _is_watchlisted(info):
        return True
    cutoff = datetime.now(tz=timezone.utc) + timedelta(days=_DISCOVERY_END_DATE_HORIZON_DAYS)
    return info.end_date <= cutoff


async def run_discovery() -> MarketDiscoveryResult:
    """
    Fetch all active markets from Gamma API and upsert them to the database.
    Only markets closing within 30 days (or with no end_date, or resolved) are written.
    Returns a summary including the list of active asset IDs for WS subscription.
    """
    logger.info("discovery_starting")

    markets: list[MarketInfo] = await fetch_active_markets(active_only=True)

    markets_upserted = 0
    tokens_upserted = 0
    markets_skipped = 0
    active_asset_ids: list[str] = []

    markets_metadata_only = 0

    factory = get_session_factory()
    async with factory() as session:
        for market in markets:
            if not _within_discovery_horizon(market):
                markets_skipped += 1
                continue
            # Markets that can't be categorized (politics, world, tech, etc.)
            # are stored WITHOUT tokens: the market row feeds the resolver and
            # the price-snapshot analytics, but no token rows means no WS
            # subscription / live trade ingestion for them.
            # run_sports_discovery() handles sports via tag_slug separately.
            categorizable = _infer_subcategory(
                market.slug, market.question, market.event_title, market.category
            ) is not None
            try:
                is_new = await _upsert_market(session, market)
                if is_new:
                    markets_upserted += 1

                if not categorizable:
                    markets_metadata_only += 1
                    continue

                for token in market.tokens:
                    if not token.token_id:
                        continue
                    is_new_token = await _upsert_token(
                        session, market.market_id, token.token_id, token.outcome
                    )
                    if is_new_token:
                        tokens_upserted += 1
                    active_asset_ids.append(token.token_id)

            except Exception as exc:
                logger.error(
                    "discovery_market_upsert_error",
                    market_id=market.market_id,
                    error=str(exc),
                )
                # Leaves the session's transaction unusable until rolled
                # back (e.g. a Postgres deadlock against a concurrent
                # discovery job writing the same market row) — without
                # this every remaining market in the batch fails
                # identically. Idempotent either way (decisions/2026-07-19.md).
                await session.rollback()

        await session.commit()

    # Deduplicate
    active_asset_ids = list(dict.fromkeys(active_asset_ids))

    result = MarketDiscoveryResult(
        markets_upserted=markets_upserted,
        tokens_upserted=tokens_upserted,
        active_asset_ids=active_asset_ids,
    )

    logger.info(
        "discovery_complete",
        markets_upserted=markets_upserted,
        tokens_upserted=tokens_upserted,
        markets_skipped=markets_skipped,
        markets_metadata_only=markets_metadata_only,
        active_assets=len(active_asset_ids),
    )

    return result


SPORTS_TAG_SLUGS = ["nhl", "nba", "mlb", "ufc", "nfl", "f1", "champions-league"]


async def run_sports_discovery() -> dict:
    """
    Discover current sports game markets via the /events?tag_slug= endpoint.

    Runs independently from run_discovery(). The main discovery job caps at
    5,000 markets via /markets pagination and misses newer game markets (IDs
    1M+). This job uses tag-based event discovery to fill that gap.

    Returns per-tag counts and totals.
    """
    from app.services.polymarket.gamma_client import fetch_sports_markets_by_tag

    total_markets_found = 0
    total_markets_upserted = 0
    total_tokens_upserted = 0
    per_tag: dict[str, dict] = {}

    factory = get_session_factory()

    for tag in SPORTS_TAG_SLUGS:
        try:
            markets = await fetch_sports_markets_by_tag(tag)
        except Exception as exc:
            logger.error("sports_discovery_tag_fetch_error", tag=tag, error=str(exc))
            per_tag[tag] = {"found": 0, "passed_horizon": 0, "upserted": 0, "tokens": 0}
            continue

        passed = 0
        upserted = 0
        tokens = 0

        async with factory() as session:
            for market in markets:
                if not _within_discovery_horizon(market):
                    continue
                passed += 1
                try:
                    is_new = await _upsert_market(session, market)
                    if is_new:
                        upserted += 1
                    for token in market.tokens:
                        if not token.token_id:
                            continue
                        is_new_token = await _upsert_token(
                            session, market.market_id, token.token_id, token.outcome
                        )
                        if is_new_token:
                            tokens += 1
                except Exception as exc:
                    logger.error(
                        "sports_discovery_upsert_error",
                        tag=tag,
                        market_id=market.market_id,
                        error=str(exc),
                    )
                    # Leaves the session's transaction unusable until rolled
                    # back (e.g. a Postgres deadlock against a concurrent
                    # discovery job writing the same market row) — without
                    # this every remaining market in the batch fails
                    # identically. Idempotent either way (decisions/2026-07-19.md).
                    await session.rollback()
            await session.commit()

        per_tag[tag] = {
            "found": len(markets),
            "passed_horizon": passed,
            "upserted": upserted,
            "tokens": tokens,
        }
        total_markets_found += len(markets)
        total_markets_upserted += upserted
        total_tokens_upserted += tokens

        logger.info(
            "sports_discovery_tag_complete",
            tag=tag,
            found=len(markets),
            passed_horizon=passed,
            upserted=upserted,
            tokens=tokens,
        )

    logger.info(
        "sports_discovery_complete",
        total_found=total_markets_found,
        total_upserted=total_markets_upserted,
        total_tokens=total_tokens_upserted,
    )
    return {
        "total_found": total_markets_found,
        "total_upserted": total_markets_upserted,
        "total_tokens": total_tokens_upserted,
        "per_tag": per_tag,
    }


async def get_active_asset_ids() -> list[str]:
    """Fetch currently active asset IDs from the DB (fast path, no Gamma call)."""
    factory = get_session_factory()
    async with factory() as session:
        result = await session.execute(
            select(Token.asset_id)
            .join(Market, Token.market_id == Market.market_id)
            .where(Market.active == True, Market.closed == False)
        )
        rows = result.scalars().all()
    return list(rows)


async def get_subscription_asset_ids(session: AsyncSession, top_n: int) -> tuple[list[str], dict]:
    """Volume-ranked WS subscription list (decisions/2026-07-18.md coverage-fix
    proposal): top-`top_n` active/unresolved/unclosed markets by `volume`,
    excluding the micro-lifecycle Up/Down family, UNION every active market any
    watched wallet currently holds an open position in regardless of volume
    rank. Returns (asset_ids, diagnostics) — diagnostics is for the projection
    report and scheduled-job logging, not for correctness.

    Markets with `volume IS NULL` sort last (never displace a market with a
    real, however small, measured volume) rather than being treated as zero.
    """
    market_rows = (
        await session.execute(
            select(Market.market_id, Market.volume, Market.slug).where(
                Market.active == True, Market.resolved == False, Market.closed == False  # noqa: E712
            )
        )
    ).all()

    n_candidate_markets = len(market_rows)
    eligible = [
        (market_id, volume, slug)
        for market_id, volume, slug in market_rows
        if not _MICRO_LIFECYCLE_SLUG_PATTERN.search(slug or "")
    ]
    n_excluded_micro_lifecycle = n_candidate_markets - len(eligible)

    eligible.sort(key=lambda row: (row[1] is None, -(row[1] or 0.0)))
    top_market_ids = {market_id for market_id, _, _ in eligible[:top_n]}

    watched_market_ids = set(
        (
            await session.execute(
                select(WalletPosition.market_id)
                .join(Wallet, Wallet.wallet == WalletPosition.wallet)
                .where(Wallet.watch_status == "watch", WalletPosition.status == "Open")
                .distinct()
            )
        ).scalars().all()
    )
    n_watched_augmented = len(watched_market_ids - top_market_ids)

    selected_market_ids = top_market_ids | watched_market_ids
    asset_ids = list(
        (
            await session.execute(
                select(Token.asset_id).where(Token.market_id.in_(selected_market_ids))
            )
        ).scalars().all()
    )

    diagnostics = {
        "n_candidate_markets": n_candidate_markets,
        "n_excluded_micro_lifecycle": n_excluded_micro_lifecycle,
        "n_top_volume_selected": len(top_market_ids),
        "n_watched_augmented": n_watched_augmented,
        "n_markets_selected": len(selected_market_ids),
        "n_assets_selected": len(asset_ids),
    }
    return asset_ids, diagnostics
