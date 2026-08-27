"""
Market resolution detection and closed position recording.

Runs every 30 minutes. Workflow:
  1. Fetch recently-resolved markets from Gamma API and sync to DB.
  2. Find markets that are resolved but have unscored BUY trades.
  3. For each such market, write WalletClosedPosition rows from open
     wallet_cost_basis entries and set outcome_result on BUY trades.
  4. Trigger a full wallet score recomputation once all markets are processed.
"""
import math
from datetime import datetime, timezone

from sqlalchemy import case as sa_case, func as sqlfunc, or_, select, text, update as sqla_update
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models.market import Market
from app.db.models.market_price_snapshot import MarketPriceSnapshot
from app.db.models.token import Token
from app.db.models.trade import Trade
from app.db.models.wallet import Wallet
from app.db.models.wallet_closed_position import WalletClosedPosition
from app.db.models.wallet_cost_basis import WalletCostBasis
from app.db.session import get_session_factory
from app.services.wallets.scorer import compute_log_roi
from app.logging import get_logger

logger = get_logger(__name__)


# ---------------------------------------------------------------------------
# Utility: Welford's online algorithm (general stats helpers — kept here
# for backwards compatibility with existing tests in test_resolution.py)
# ---------------------------------------------------------------------------

def _welford_update(
    n: int, mean: float, m2: float, new_value: float
) -> tuple[int, float, float]:
    """One Welford step. Returns (n+1, new_mean, new_M2)."""
    n += 1
    delta = new_value - mean
    mean += delta / n
    delta2 = new_value - mean
    m2 += delta * delta2
    return n, mean, m2


def _welford_stdev(n: int, m2: float) -> float:
    """Sample standard deviation from Welford running state."""
    return math.sqrt(m2 / (n - 1)) if n >= 2 else 0.0


# ---------------------------------------------------------------------------
# Snapshot helper: mark price snapshots resolved when market settles
# ---------------------------------------------------------------------------

async def _update_snapshots_on_resolution(
    session: AsyncSession,
    market_id: str,
    resolution: str,
) -> None:
    """Update market_price_snapshots rows for a newly resolved market."""
    snap_result = await session.execute(
        select(MarketPriceSnapshot).where(
            MarketPriceSnapshot.market_id == market_id,
            MarketPriceSnapshot.resolved == False,  # noqa: E712
        )
    )
    snapshots = snap_result.scalars().all()
    for snap in snapshots:
        snap.resolved = True
        snap.resolution = resolution
        snap.high_side_won = snap.high_side == resolution
    if snapshots:
        logger.debug(
            "snapshots_resolved",
            market_id=market_id,
            resolution=resolution,
            count=len(snapshots),
        )


# ---------------------------------------------------------------------------
# Step 1: sync newly resolved markets from Gamma
# ---------------------------------------------------------------------------

async def _sync_resolved_markets(session: AsyncSession) -> list[str]:
    """
    Check candidate markets against Gamma to see if they now have a resolved
    outcome. Two candidate tracks:

      Track 1 ("due") — unresolved markets whose end_date has passed. Checked
      every run; this is the common case (market concluded on schedule).

      Track 2 ("sweep") — unresolved, still-active markets whose end_date is
      still in the future, round-robin ordered by resolution_checked_at.
      Catches markets that resolve *before* their nominal end_date: "will X
      win the tournament/election" outright-winner markets carry the event's
      overall end_date, not the date a given entrant's fate is decided (e.g.
      eliminated from a bracket) — Track 1 alone never looks at them again
      once end_date passes the check, because it never arrives. Confirmed via
      sample: 2/40 (5%) of markets in this state were already closed on Gamma.
      See decisions/2026-07-17.md.

    Every candidate checked (either track, resolved or not) gets
    resolution_checked_at stamped, so the sweep makes forward progress
    through the whole unresolved-but-not-yet-due population over time
    instead of hammering the same head-of-queue markets.

    Returns list of market_ids newly marked as resolved.
    """
    from app.services.polymarket.gamma_client import fetch_market_by_id

    now = datetime.now(tz=timezone.utc)

    due_rows = await session.execute(
        select(Market.market_id)
        .where(
            Market.resolved == False,  # noqa: E712
            Market.end_date < now,
        )
        .order_by(Market.end_date.desc())
        .limit(50)
    )
    due_ids = [row[0] for row in due_rows.all()]

    sweep_rows = await session.execute(
        select(Market.market_id)
        .where(
            Market.resolved == False,  # noqa: E712
            Market.active == True,  # noqa: E712
            Market.end_date >= now,
        )
        .order_by(Market.resolution_checked_at.asc().nulls_first())
        .limit(50)
    )
    sweep_ids = [row[0] for row in sweep_rows.all()]

    candidate_ids = due_ids + sweep_ids
    if not candidate_ids:
        logger.debug("resolution_no_candidates")
        return []

    logger.info(
        "resolution_checking_candidates",
        due=len(due_ids),
        sweep=len(sweep_ids),
    )

    newly_resolved: list[str] = []
    for market_id in candidate_ids:
        info = await fetch_market_by_id(market_id)

        result = await session.execute(
            select(Market).where(Market.market_id == market_id)
        )
        market = result.scalar_one_or_none()
        if market is None:
            continue
        market.resolution_checked_at = now

        if info is None:
            continue
        if not info.resolved or not info.resolution:
            continue

        if not market.resolved:
            market.resolved = True
            market.resolution = info.resolution
            market.active = False
            market.closed = True
            market.updated_at = now
            # Gamma's own settlement time, never our end_date — see
            # docs/m1-schema-audit.md. Falls back to detection time (now),
            # never to end_date, so closed_at is never earlier than truth.
            market.resolved_at = info.resolved_at or now
            newly_resolved.append(market_id)
            logger.info(
                "resolution_market_resolved",
                market_id=market_id,
                resolution=info.resolution,
            )
            await _update_snapshots_on_resolution(session, market_id, info.resolution)
        elif not market.resolution and info.resolution:
            market.resolution = info.resolution
            market.active = False
            market.closed = True
            market.updated_at = now
            if market.resolved_at is None:
                market.resolved_at = info.resolved_at or now
            await _update_snapshots_on_resolution(session, market_id, info.resolution)

    # Always commit — resolution_checked_at stamps must persist even when
    # nothing newly resolved in this batch, or the sweep track (Track 2)
    # re-checks the same candidates forever instead of making forward
    # progress through the not-yet-due population.
    await session.commit()
    if newly_resolved:
        logger.info("resolution_markets_newly_resolved", count=len(newly_resolved))

    return newly_resolved


# ---------------------------------------------------------------------------
# Step 2: find markets needing processing
# ---------------------------------------------------------------------------

async def _find_markets_needing_restamp(session: AsyncSession) -> list[str]:
    """
    Find market_ids needing a trades.outcome_result pass. Two independent
    triggers, OR'd together — they catch different failure modes and neither
    subsumes the other:

      (A) Trade.outcome_result IS NULL — the original trigger: a BUY trade
          that has never been stamped (a freshly-resolved market's first
          pass, or a trade that simply arrived/attributed after its market
          was already fully processed).

      (B) Market.resolution IS DISTINCT FROM outcome_result_stamped_resolution
          — the chokepoint fix for decisions/2026-08-06.md: markets.resolution
          changed after trades were already stamped against the old value.
          The old code only had trigger (A), so any post-hoc correction to
          markets.resolution left already-stamped (non-NULL) trades frozen
          inverted forever — the 9-market, 15,189-trade incident this exists
          to close.

    Caught the hard way: migration 0034 backfills
    outcome_result_stamped_resolution to the CURRENT resolution for every
    already-resolved market, so on its own trigger (B) would go silent for
    any market that already has its resolution correctly recorded — even one
    that still has genuinely NULL, never-stamped trades sitting on it (295
    such markets found live 2026-08-06: routine backlog, no open
    wallet_cost_basis on any of them, so no WCP work was actually being
    silently skipped, but their outcome_result would have sat NULL forever
    with neither trigger able to see them). Trigger (A) staying in this OR is
    what keeps that population visible to the standing job regardless of
    what migration 0034 backfilled.

    Scoped to markets with at least one eligible BUY trade so a correction
    event only re-processes that market's own rows, never the full table —
    see migrations/versions/0034 for the index trigger (B) relies on.

    Excludes resolved=True/closed=False markets (the known 34-row
    ck_markets_resolved_implies_closed violation, migration 0033 / punch list
    item 13) — Postgres re-checks that NOT VALID constraint on any UPDATE to
    an already-violating row, so writing outcome_result_stamped_resolution to
    one of them would raise IntegrityError every cycle. Left untouched, same
    as migration 0034's backfill.
    """
    rows = await session.execute(
        select(Market.market_id)
        .join(Trade, Trade.market_id == Market.market_id)
        .where(
            Market.resolved == True,  # noqa: E712
            Market.closed == True,  # noqa: E712
            Market.resolution.is_not(None),
            or_(
                Trade.outcome_result.is_(None),
                Market.resolution.is_distinct_from(Market.outcome_result_stamped_resolution),
            ),
            Trade.side == "BUY",
            Trade.wallet.is_not(None),
            Trade.trade_type.is_(None),
        )
        .distinct()
    )
    return [r.market_id for r in rows.all()]


async def _restamp_trade_outcome_results(
    session: AsyncSession, market_id: str, resolution_str: str
) -> int:
    """
    Backfill trades.outcome from tokens.outcome where missing (never fabricate
    a result off a blank outcome), then (re)stamp outcome_result for every
    eligible BUY trade on this market against the given (already-uppercased)
    resolution. Safe to call repeatedly — idempotent once outcome_result
    already agrees. Returns the number of rows the stamping UPDATE touched.
    """
    await session.execute(
        text(
            """
            UPDATE trades t
            SET outcome = tok.outcome
            FROM tokens tok
            WHERE tok.asset_id = t.asset_id
              AND t.market_id = :market_id
              AND t.side = 'BUY'
              AND t.outcome IS NULL
              AND tok.outcome IS NOT NULL
            """
        ),
        {"market_id": market_id},
    )

    result = await session.execute(
        sqla_update(Trade)
        .where(
            Trade.market_id == market_id,
            Trade.side == "BUY",
            Trade.wallet.is_not(None),
            Trade.trade_type.is_(None),
            Trade.price.is_not(None),
        )
        .values(
            outcome_result=sa_case(
                (Trade.outcome.is_(None), None),
                (sqlfunc.upper(Trade.outcome) == resolution_str, 1),
                else_=0,
            )
        )
        .execution_options(synchronize_session=False)
    )
    return result.rowcount


# ---------------------------------------------------------------------------
# Step 3: process a single resolved market
# ---------------------------------------------------------------------------

async def _process_market_resolution(
    session: AsyncSession,
    market_id: str,
) -> set[str]:
    """
    For a resolved market:
    - Write WalletClosedPosition rows from wallet_cost_basis open positions.
    - Delete processed wallet_cost_basis rows (position fully closed by resolution).
    - Set outcome_result on all BUY trades (for UI display).
    - Close wallet_positions rows for affected wallets.
    - Record the resolution value used, so a later correction to
      markets.resolution is detected and this market is re-processed.
    Returns set of wallet addresses that were updated.

    Re-reads Market fresh here rather than trusting a resolution string
    captured earlier by the caller (decisions/2026-08-06.md) — closes a TOCTOU
    race where anything that changed markets.resolution between the caller's
    read and this write (a concurrent correction, or just candidate-list
    processing order/latency within the same job run) would otherwise get
    silently baked into both the trades stamp and the WCP exit_price below.
    """
    from app.services.wallets.positions import close_position_on_resolution
    now = datetime.now(tz=timezone.utc)

    mkt_result = await session.execute(
        select(Market.resolution, Market.resolved_at).where(Market.market_id == market_id)
    )
    mkt_row = mkt_result.one_or_none()
    if mkt_row is None or mkt_row.resolution is None:
        logger.warning("resolution_market_missing_or_unresolved", market_id=market_id)
        return set()
    resolution_str = mkt_row.resolution.upper()

    # closed_at = true settlement time (never end_date — see docs/m1-schema-audit.md).
    # Falls back to detection time (now), never to end_date, so a position is
    # never treated as closed earlier than we could actually have known.
    resolved_at_value = mkt_row.resolved_at
    market_resolved_at = resolved_at_value or now
    closed_at_source = "gamma_settlement" if resolved_at_value is not None else "detection_time_proxy"

    # Load all open cost-basis positions for this market
    pos_result = await session.execute(
        select(WalletCostBasis).where(WalletCostBasis.market_id == market_id)
    )
    positions = pos_result.scalars().all()

    # Preload token outcomes for the market (avoids one query per position)
    outcome_by_asset: dict[str, str] = {
        r.asset_id: r.outcome
        for r in (await session.execute(
            select(Token.asset_id, Token.outcome).where(Token.market_id == market_id)
        )).all()
        if r.outcome is not None
    }

    affected_wallets: set[str] = set()
    lifetime_cache: dict[str, float] = {}

    for pos in positions:
        if pos.avg_entry_price <= 0 or pos.total_size <= 0:
            await session.delete(pos)
            continue

        token_outcome = outcome_by_asset.get(pos.asset_id)
        if token_outcome is None:
            logger.warning(
                "resolution_missing_token_outcome",
                asset_id=pos.asset_id,
                market_id=market_id,
            )
            await session.delete(pos)
            continue

        outcome_str = token_outcome.upper()
        exit_price = 1.0 if outcome_str == resolution_str else 0.0

        log_roi_val = compute_log_roi(exit_price, pos.avg_entry_price) or 0.0

        cost_basis_val = pos.total_size * pos.avg_entry_price
        realized_pnl_val = (exit_price - pos.avg_entry_price) * pos.total_size

        # Wallet lifetime BUY-only notional for conviction weight (cached per wallet)
        if pos.wallet not in lifetime_cache:
            lifetime_row = await session.execute(
                select(sqlfunc.sum(Trade.notional_usd))
                .where(Trade.wallet == pos.wallet, Trade.notional_usd.is_not(None), Trade.side == "BUY")
            )
            lifetime_cache[pos.wallet] = float(lifetime_row.scalar_one_or_none() or 0.0)
        lifetime_notional = lifetime_cache[pos.wallet]
        conviction = cost_basis_val / lifetime_notional if lifetime_notional > 0 else 0.0

        session.add(WalletClosedPosition(
            wallet=pos.wallet,
            asset_id=pos.asset_id,
            market_id=market_id,
            entry_price=pos.avg_entry_price,
            exit_price=exit_price,
            shares_sold=pos.total_size,
            total_shares_at_time=pos.total_size,
            position_fraction=1.0,
            cost_basis=cost_basis_val,
            conviction_weight=conviction,
            log_roi=log_roi_val,
            realized_pnl=realized_pnl_val,
            is_resolved=True,
            closed_at=market_resolved_at,
            closed_at_source=closed_at_source,
        ))

        await close_position_on_resolution(
            session,
            wallet=pos.wallet,
            asset_id=pos.asset_id,
            realized_pnl=realized_pnl_val,
            remaining_shares=pos.total_size,
            closed_at=market_resolved_at,
        )

        await session.delete(pos)
        affected_wallets.add(pos.wallet)

    # Set outcome_result on every BUY trade for this market (bulk update) —
    # not gated on outcome_result IS NULL, so a resolution correction
    # re-stamps rows that were already (wrongly) stamped, not just new ones.
    await _restamp_trade_outcome_results(session, market_id, resolution_str)

    # Record what we stamped against, so a later change to markets.resolution
    # is detected by _find_markets_needing_restamp — the actual chokepoint fix.
    await session.execute(
        sqla_update(Market)
        .where(Market.market_id == market_id)
        .values(outcome_result_stamped_resolution=mkt_row.resolution)
    )

    return affected_wallets


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------

async def check_and_process_resolved_markets() -> dict:
    """
    Main entry point for the resolution detection job.
    Returns summary dict with counts.
    """
    markets_synced = 0
    markets_processed = 0
    wallets_updated = 0
    wallets_rescored = 0

    factory = get_session_factory()
    async with factory() as session:
        newly_resolved = await _sync_resolved_markets(session)
        markets_synced = len(newly_resolved)
        market_ids = await _find_markets_needing_restamp(session)

    for market_id in market_ids:
        try:
            factory = get_session_factory()
            async with factory() as session:
                affected = await _process_market_resolution(session, market_id)
                await session.commit()
            wallets_updated += len(affected)
            markets_processed += 1
        except Exception as exc:
            logger.error(
                "resolution_market_error",
                market_id=market_id,
                error=str(exc),
                exc_info=True,
            )

    # Full score recomputation once after all markets are processed
    if wallets_updated > 0:
        try:
            from app.services.wallets.scorer import run_all_wallet_scores
            factory = get_session_factory()
            async with factory() as session:
                result = await run_all_wallet_scores(session)
                await session.commit()
            wallets_rescored = result.get("wallets_scored", 0)
        except Exception as exc:
            logger.error("resolution_rescore_error", error=str(exc), exc_info=True)

    logger.info(
        "resolution_check_complete",
        markets_synced=markets_synced,
        markets_processed=markets_processed,
        wallets_updated=wallets_updated,
        wallets_rescored=wallets_rescored,
    )
    return {
        "markets_synced": markets_synced,
        "markets_processed": markets_processed,
        "wallets_updated": wallets_updated,
        "wallets_rescored": wallets_rescored,
    }
