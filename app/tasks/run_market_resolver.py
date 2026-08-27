"""
Market auto-resolver — backfills resolution for markets past their end_date.

Queries markets where:
  - end_date < now - 24h  (give Polymarket time to post the result)
  - resolved = False
  (inactive markets are included — a market only leaves the candidate set
   once resolved, so temporary Gamma outages never permanently strand it)

Resolution attempt chain:

  1. Gamma API  GET /markets?id=A&id=B&...  (batched, 100 per request)
     → resolution derived from closed=true + degenerate outcomePrices
       (Gamma has no `resolved`/`resolution` fields).

  2. CLOB API  GET {clob_base_url}/markets/{condition_id}
     → tried when Gamma returns nothing; tokens[].winner is authoritative.

  3. Local DB price check
     → inspect WalletClosedPosition.exit_price and Trade.price for tokens
        in this market; a price of 1.0 identifies the winning outcome.

  4. Mark stale
     → end_date < now - 48h and still unresolved: active=False (drops the
       market from WS subscriptions; it remains a resolver candidate).
     → end_date < now - 7 days and no resolution found anywhere: also close
       open positions with unknown outcome (no PnL, no WCP row).
        Never marks resolved=True without a confirmed outcome label.

Does NOT trigger wallet scoring — the existing resolution.py job handles that
and already picks up the newly-resolved markets on its next 30-minute cycle.

Manual run:
  python -m app.tasks.run_market_resolver

Scheduled:
  market_resolver  every 6 hours  (registered in run_worker.py)
"""
import asyncio
import sys
from datetime import datetime, timedelta, timezone

import httpx
from sqlalchemy import func as sqlfunc, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.services.polymarket.gamma_client import derive_resolution, fetch_markets_raw_by_ids
from app.services.wallets.scorer import compute_log_roi

from app.db.models.market import Market
from app.db.models.market_price_snapshot import MarketPriceSnapshot
from app.db.models.token import Token
from app.db.models.trade import Trade
from app.db.models.wallet_closed_position import WalletClosedPosition
from app.db.models.wallet_cost_basis import WalletCostBasis
from app.db.models.wallet_position import WalletPosition
from app.db.session import get_session_factory
from app.logging import setup_logging, get_logger

logger = get_logger(__name__)

_GRACE_HOURS = 24        # markets must be at least this many hours past end_date
_STALE_HOURS = 48        # mark inactive when still-unresolved after this
_NO_DATA_STALE_DAYS = 7  # close positions with unknown outcome after this many days


# ---------------------------------------------------------------------------
# Fallback Step 1 — CLOB API (tokens[].winner is the authoritative signal)
# ---------------------------------------------------------------------------

async def _try_clob_api(condition_id: str | None) -> tuple[bool, str | None]:
    """
    Try to get resolution from the CLOB API using the market's condition_id.

    GET {clob_base_url}/markets/{condition_id} returns per-token
    winner: true/false flags once the market has settled.

    Returns (is_resolved, resolution_label). Both False/None on failure or no data.
    """
    if not condition_id:
        return False, None

    from app.config import get_settings
    settings = get_settings()
    url = f"{settings.polymarket_clob_base_url}/markets/{condition_id}"
    try:
        async with httpx.AsyncClient(timeout=15.0, verify=settings.gamma_ssl_verify) as client:
            resp = await client.get(url)
            if resp.status_code == 404:
                return False, None
            if resp.status_code != 200:
                logger.debug(
                    "resolver_clob_non200",
                    condition_id=condition_id[:20],
                    status=resp.status_code,
                )
                return False, None

            data = resp.json()
            if not isinstance(data, dict):
                return False, None

            winner_label: str | None = None
            for tok in data.get("tokens") or []:
                if tok.get("winner"):
                    winner_label = tok.get("outcome") or tok.get("name")
                    break

            resolved = bool(data.get("closed")) and winner_label is not None

            logger.debug(
                "resolver_clob_response",
                condition_id=condition_id[:20],
                resolved=resolved,
                resolution=winner_label,
            )
            return resolved, winner_label if resolved else None

    except Exception as exc:
        logger.warning(
            "resolver_clob_api_error",
            condition_id=condition_id[:20] if condition_id else "",
            error=str(exc),
        )
        return False, None


# ---------------------------------------------------------------------------
# Fallback Step 2 — Local DB price check
# ---------------------------------------------------------------------------

async def _try_resolve_from_db(session: AsyncSession, market_id: str) -> str | None:
    """
    Determine resolution by inspecting local trade/WCP data.

    A WalletClosedPosition.exit_price of 1.0 means that token's outcome is the winner.
    A SELL Trade.price of 1.0 is a resolution payout — same conclusion.

    Returns the winning outcome label, or None if the signal is absent or ambiguous.
    """
    # Get tokens for this market
    tok_rows = (await session.execute(
        select(Token.asset_id, Token.outcome)
        .where(Token.market_id == market_id)
    )).all()

    if not tok_rows:
        return None

    outcome_by_asset: dict[str, str] = {
        r.asset_id: r.outcome for r in tok_rows if r.outcome
    }
    if not outcome_by_asset:
        return None

    asset_ids = list(outcome_by_asset)

    # Check WCPs first — exit_price is explicitly set at resolution time
    wcp_assets = (await session.execute(
        select(WalletClosedPosition.asset_id).where(
            WalletClosedPosition.market_id == market_id,
            WalletClosedPosition.exit_price >= 0.99,
        ).distinct()
    )).scalars().all()

    if wcp_assets:
        outcomes = {outcome_by_asset[aid] for aid in wcp_assets if aid in outcome_by_asset}
        if len(outcomes) == 1:
            resolution = outcomes.pop()
            logger.debug(
                "resolver_db_wcp_hit",
                market_id=market_id,
                resolution=resolution,
            )
            return resolution

    # Fallback: check SELL trades at price=1.0
    trade_assets = (await session.execute(
        select(Trade.asset_id).where(
            Trade.asset_id.in_(asset_ids),
            Trade.side == "SELL",
            Trade.price >= 0.99,
        ).distinct()
    )).scalars().all()

    if trade_assets:
        outcomes = {outcome_by_asset[aid] for aid in trade_assets if aid in outcome_by_asset}
        if len(outcomes) == 1:
            resolution = outcomes.pop()
            logger.debug(
                "resolver_db_trade_hit",
                market_id=market_id,
                resolution=resolution,
            )
            return resolution

    return None


# ---------------------------------------------------------------------------
# Snapshot update helper
# ---------------------------------------------------------------------------

async def _update_snapshots(session: AsyncSession, market_id: str, resolution: str) -> int:
    """Mark unresolved price snapshots for this market as resolved. Returns count updated."""
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
    return len(snapshots)


# ---------------------------------------------------------------------------
# Retroactive snapshot helpers
# ---------------------------------------------------------------------------

async def _wcp_avg_entry_for_winner(
    session: AsyncSession, market_id: str, resolution: str
) -> float | None:
    """
    Return the average entry_price from resolved WCP rows where the outcome
    matches the winning resolution.  Used as a proxy for the pre-resolution
    market price.  Returns None if no matching WCPs exist.
    """
    wcp_rows = (await session.execute(
        select(WalletClosedPosition.entry_price, Token.outcome)
        .join(Token, Token.asset_id == WalletClosedPosition.asset_id)
        .where(
            WalletClosedPosition.market_id == market_id,
            WalletClosedPosition.is_resolved == True,  # noqa: E712
        )
    )).all()
    if not wcp_rows:
        return None

    resolution_lower = resolution.strip().lower()
    winning_entries = [
        entry_price for entry_price, outcome in wcp_rows
        if outcome and outcome.strip().lower() == resolution_lower
    ]

    if not winning_entries:
        all_entries = [ep for ep, _ in wcp_rows]
        return sum(all_entries) / len(all_entries) if all_entries else None

    return sum(winning_entries) / len(winning_entries)


async def _create_retroactive_snapshot(
    session: AsyncSession,
    market_id: str,
    resolution: str,
    end_date: datetime,
    high_side_price: float,
) -> bool:
    """
    Write a retroactive T24 snapshot for a market that has already resolved.
    high_side is always the winning outcome (resolution label).
    high_side_won is always True.
    snapshot_ts = end_date - 12h (estimated pre-close capture).
    Returns True if written, False if skipped (already exists or error).
    """
    snapshot_ts = end_date - timedelta(hours=12)
    if snapshot_ts.tzinfo is None:
        snapshot_ts = snapshot_ts.replace(tzinfo=timezone.utc)

    other_price = round(1.0 - high_side_price, 4)

    try:
        session.add(MarketPriceSnapshot(
            market_id=market_id,
            snapshot_ts=snapshot_ts,
            hours_to_close=12.0,
            snapshot_bucket="T24",
            yes_price=high_side_price,
            no_price=other_price,
            high_side=resolution,
            high_side_price=high_side_price,
            resolved=True,
            resolution=resolution,
            high_side_won=True,
        ))
        await session.flush()
        logger.info(
            "retroactive_snapshot_created",
            market_id=market_id,
            resolution=resolution,
            high_side_price=high_side_price,
        )
        return True
    except IntegrityError:
        await session.rollback()
        logger.debug("retroactive_snapshot_duplicate", market_id=market_id)
        return False


async def backfill_retroactive_snapshots() -> dict:
    """
    Create retroactive T24 snapshots for resolved markets that have WCP data
    but no existing snapshot rows.

    Intended for one-time backfill of markets resolved before the snapshot
    job was running.  Safe to run multiple times — skips duplicates.
    """
    factory = get_session_factory()
    created = 0
    skipped = 0

    async with factory() as session:
        existing_snap_ids = select(MarketPriceSnapshot.market_id).distinct()
        candidate_rows = await session.execute(
            select(Market.market_id, Market.resolution, Market.end_date, Market.question)
            .where(
                Market.resolved == True,  # noqa: E712
                Market.resolution.is_not(None),
                Market.end_date.is_not(None),
                Market.volume >= 10_000,
                ~Market.market_id.in_(existing_snap_ids),
            )
            .order_by(Market.end_date.asc())
        )
        candidates = candidate_rows.all()

    logger.info("retroactive_backfill_candidates", count=len(candidates))

    for row in candidates:
        market_id = row.market_id
        resolution = row.resolution
        end_date = row.end_date
        if end_date.tzinfo is None:
            end_date = end_date.replace(tzinfo=timezone.utc)

        async with factory() as session:
            high_side_price = await _wcp_avg_entry_for_winner(session, market_id, resolution)
            if high_side_price is None or high_side_price < 0.80:
                skipped += 1
                logger.debug(
                    "retroactive_backfill_skipped",
                    market_id=market_id,
                    reason="no_wcp_or_below_threshold",
                    high_side_price=high_side_price,
                )
                continue

            ok = await _create_retroactive_snapshot(
                session, market_id, resolution, end_date, high_side_price
            )
            await session.commit()
            if ok:
                created += 1
            else:
                skipped += 1

    logger.info("retroactive_backfill_complete", created=created, skipped=skipped)
    return {"created": created, "skipped": skipped}


# ---------------------------------------------------------------------------
# Position closure helpers
# ---------------------------------------------------------------------------

async def _close_positions_on_resolution(
    session: AsyncSession,
    market_id: str,
    resolution: str,
    now: datetime,
) -> int:
    """
    Close all open wallet_positions for a resolved market with correct PnL.

    Winning outcome → exit_price=1.0, pnl=(1.0-entry)*shares
    Losing outcome  → exit_price=0.0, pnl=(0.0-entry)*shares

    Skips writing a WCP row if a resolved WCP for (wallet, asset_id, market_id)
    already exists. Returns count of positions closed.
    """
    pos_result = await session.execute(
        select(WalletPosition).where(
            WalletPosition.market_id == market_id,
            WalletPosition.remaining_shares > 0,
        )
    )
    positions = pos_result.scalars().all()
    if not positions:
        return 0

    tok_result = await session.execute(
        select(Token.asset_id, Token.outcome).where(Token.market_id == market_id)
    )
    outcome_by_asset: dict[str, str] = {
        r.asset_id: r.outcome for r in tok_result.all() if r.outcome
    }

    closed = 0
    for pos in positions:
        pos_outcome = outcome_by_asset.get(pos.asset_id, "")
        is_winner = bool(pos_outcome) and pos_outcome.strip().lower() == resolution.strip().lower()
        exit_price = 1.0 if is_winner else 0.0
        shares = pos.remaining_shares

        cb_result = await session.execute(
            select(WalletCostBasis).where(
                WalletCostBasis.wallet == pos.wallet,
                WalletCostBasis.asset_id == pos.asset_id,
            )
        )
        cb = cb_result.scalar_one_or_none()
        entry_price = (
            cb.avg_entry_price
            if cb and cb.avg_entry_price > 0
            else pos.avg_entry_price
        )
        realized_pnl = (exit_price - entry_price) * shares

        # Write WCP unless a resolved one already exists for this position
        dup_count = (await session.execute(
            select(sqlfunc.count())
            .select_from(WalletClosedPosition)
            .where(
                WalletClosedPosition.wallet == pos.wallet,
                WalletClosedPosition.asset_id == pos.asset_id,
                WalletClosedPosition.market_id == market_id,
                WalletClosedPosition.is_resolved == True,  # noqa: E712
            )
        )).scalar_one()

        if dup_count == 0:
            cost_basis_val = entry_price * shares
            log_roi_val = compute_log_roi(exit_price, entry_price) or 0.0
            lifetime_row = await session.execute(
                select(sqlfunc.sum(Trade.notional_usd)).where(
                    Trade.wallet == pos.wallet,
                    Trade.notional_usd.is_not(None),
                    Trade.side == "BUY",
                )
            )
            lifetime_notional = float(lifetime_row.scalar_one_or_none() or 0.0)
            conviction = cost_basis_val / lifetime_notional if lifetime_notional > 0 else 0.0

            session.add(WalletClosedPosition(
                wallet=pos.wallet,
                asset_id=pos.asset_id,
                market_id=market_id,
                entry_price=entry_price,
                exit_price=exit_price,
                shares_sold=shares,
                total_shares_at_time=shares,
                position_fraction=1.0,
                cost_basis=cost_basis_val,
                conviction_weight=conviction,
                log_roi=log_roi_val,
                realized_pnl=realized_pnl,
                is_resolved=True,
                closed_at=now,
            ))

        # Update wallet_position
        pos.total_shares_sold += shares
        pos.remaining_shares = 0.0
        pos.realized_pnl = (pos.realized_pnl or 0.0) + realized_pnl
        pos.status = "Closed"
        pos.closed_at = now

        if cb:
            await session.delete(cb)

        logger.info(
            "position_closed_on_resolution",
            wallet=pos.wallet,
            market_id=market_id,
            asset_id=pos.asset_id[:20],
            outcome=pos_outcome,
            is_winner=is_winner,
            entry_price=round(entry_price, 4),
            exit_price=exit_price,
            shares=round(shares, 4),
            realized_pnl=round(realized_pnl, 4),
        )
        closed += 1

    return closed


async def _close_positions_unknown_outcome(
    session: AsyncSession,
    market_id: str,
    now: datetime,
) -> int:
    """
    Close open positions for a stale market with unknown outcome.
    Sets remaining_shares=0, realized_pnl unchanged. No WCP row written.
    Returns count of positions closed.
    """
    pos_result = await session.execute(
        select(WalletPosition).where(
            WalletPosition.market_id == market_id,
            WalletPosition.remaining_shares > 0,
        )
    )
    positions = pos_result.scalars().all()
    closed = 0
    for pos in positions:
        pos.total_shares_sold += pos.remaining_shares
        pos.remaining_shares = 0.0
        pos.status = "Closed"
        pos.closed_at = now
        logger.info(
            "position_closed_unknown_outcome",
            wallet=pos.wallet,
            market_id=market_id,
            asset_id=pos.asset_id[:20],
        )
        closed += 1
    return closed


# ---------------------------------------------------------------------------
# Shared resolution applier
# ---------------------------------------------------------------------------

async def _apply_resolution(
    session: AsyncSession,
    market: Market,
    resolution: str,
    end_date: datetime,
    now: datetime,
    source: str,
) -> int:
    """Mark a market as resolved, update snapshots, and close open positions. Returns positions closed."""
    market.resolved = True
    market.resolution = resolution
    market.active = False
    market.closed = True
    market.updated_at = now
    snap_count = await _update_snapshots(session, market.market_id, resolution)

    if snap_count == 0:
        end_dt = end_date
        if end_dt and end_dt.tzinfo is None:
            end_dt = end_dt.replace(tzinfo=timezone.utc)
        high_side_price = await _wcp_avg_entry_for_winner(
            session, market.market_id, resolution
        )
        if high_side_price is not None and high_side_price >= 0.80 and end_dt:
            await _create_retroactive_snapshot(
                session, market.market_id, resolution, end_dt, high_side_price
            )

    positions_closed = await _close_positions_on_resolution(
        session, market.market_id, resolution, now
    )

    logger.info(
        "market_resolved",
        source=source,
        market_id=market.market_id,
        resolution=resolution,
        snapshots_updated=snap_count,
        positions_closed=positions_closed,
    )
    return positions_closed


# ---------------------------------------------------------------------------
# Per-market coroutine (run concurrently via asyncio.gather)
# ---------------------------------------------------------------------------

_CONCURRENCY = 20

async def _process_one_market(
    row,
    raw: dict | None,
    factory,
    semaphore: asyncio.Semaphore,
    now: datetime,
    stale_cutoff: datetime,
    no_data_stale_cutoff: datetime,
) -> dict:
    """
    Process a single candidate market through the resolution fallback chain.
    `raw` is the pre-fetched Gamma dict (from the batched lookup), or None if
    Gamma no longer serves this market.
    Guarded by semaphore to cap concurrent API calls / DB sessions.
    Returns per-market counts.
    """
    result = {
        "resolved": 0, "inactive": 0, "unknown_closed": 0, "skipped": 0, "errors": 0,
        "fallback_clob": 0, "fallback_db": 0, "positions_closed": 0,
    }

    market_id = row.market_id
    condition_id = row.condition_id
    end_date = row.end_date
    question = row.question or market_id

    if end_date and end_date.tzinfo is None:
        end_date = end_date.replace(tzinfo=timezone.utc)

    async with semaphore:
        try:
            resolution: str | None = None
            source: str | None = None

            # ── Step 0: pre-fetched Gamma data ─────────────────────────────
            if raw is not None:
                is_resolved, gamma_resolution = derive_resolution(raw)
                if is_resolved and gamma_resolution:
                    resolution = gamma_resolution
                    source = "gamma"
            else:
                # ── Gamma miss: fallback chain ─────────────────────────────
                logger.debug("resolver_gamma_miss", market_id=market_id)

                clob_resolved, clob_resolution = await _try_clob_api(condition_id)
                if clob_resolved and clob_resolution:
                    resolution = clob_resolution
                    source = "clob_api"
                    result["fallback_clob"] += 1

                if resolution is None:
                    async with factory() as session:
                        db_resolution = await _try_resolve_from_db(session, market_id)
                    if db_resolution:
                        resolution = db_resolution
                        source = "db_price"
                        result["fallback_db"] += 1

            # ── Apply resolution or walk the stale ladder ──────────────────
            async with factory() as session:
                market = (await session.execute(
                    select(Market).where(Market.market_id == market_id)
                )).scalar_one_or_none()
                if market is None:
                    result["skipped"] += 1
                    return result

                if resolution and source:
                    n = await _apply_resolution(session, market, resolution, end_date, now, source)
                    await session.commit()
                    result["resolved"] += 1
                    result["positions_closed"] += n
                    return result

                # Unresolved. Sync closed flag from Gamma if it says trading ended.
                if raw is not None and raw.get("closed") and not market.closed:
                    market.closed = True
                    market.updated_at = now

                if end_date and end_date < no_data_stale_cutoff:
                    # A week past end with no outcome anywhere — give up on the
                    # positions (unknown outcome, no PnL). Market stays
                    # resolved=False so a late settlement can still be picked up.
                    changed_active = market.active
                    market.active = False
                    market.updated_at = now
                    n = await _close_positions_unknown_outcome(session, market_id, now)
                    await session.commit()
                    if changed_active:
                        result["inactive"] += 1
                    if n:
                        result["unknown_closed"] += 1
                        result["positions_closed"] += n
                        logger.info(
                            "market_positions_closed_unknown",
                            market_id=market_id,
                            question=question,
                            end_date=str(end_date),
                            positions_closed=n,
                        )
                elif end_date and end_date < stale_cutoff:
                    # 48h past end and unresolved — drop from WS subscriptions
                    # but leave positions open; resolution is still pending.
                    if market.active:
                        market.active = False
                        market.updated_at = now
                        result["inactive"] += 1
                        logger.info(
                            "market_marked_inactive",
                            market_id=market_id,
                            question=question,
                        )
                    else:
                        result["skipped"] += 1
                    await session.commit()
                else:
                    result["skipped"] += 1

        except Exception as exc:
            logger.error(
                "resolver_market_error",
                market_id=market_id,
                error=str(exc),
                exc_info=True,
            )
            result["errors"] += 1

    return result


# ---------------------------------------------------------------------------
# Main resolution loop
# ---------------------------------------------------------------------------

async def run_market_resolver() -> dict:
    """
    Resolve or deactivate markets past their end_date.
    Processes candidates concurrently (up to _CONCURRENCY at a time).
    Returns summary dict with counts.
    """
    now = datetime.now(tz=timezone.utc)
    grace_cutoff = now - timedelta(hours=_GRACE_HOURS)
    stale_cutoff = now - timedelta(hours=_STALE_HOURS)
    no_data_stale_cutoff = now - timedelta(days=_NO_DATA_STALE_DAYS)

    factory = get_session_factory()

    async with factory() as session:
        rows = await session.execute(
            select(Market.market_id, Market.condition_id, Market.end_date, Market.question)
            .where(
                Market.resolved == False,  # noqa: E712
                Market.end_date.is_not(None),
                Market.end_date < grace_cutoff,
            )
            .order_by(Market.end_date.asc())
        )
        candidates = rows.all()

    logger.info("resolver_candidates", count=len(candidates))

    # Batched Gamma lookup for all candidates (100 IDs per request)
    raw_by_id = await fetch_markets_raw_by_ids([row.market_id for row in candidates])
    logger.info(
        "resolver_gamma_batch_fetched",
        candidates=len(candidates),
        found_in_gamma=len(raw_by_id),
    )

    semaphore = asyncio.Semaphore(_CONCURRENCY)
    market_results = await asyncio.gather(*[
        _process_one_market(
            row, raw_by_id.get(row.market_id), factory, semaphore,
            now, stale_cutoff, no_data_stale_cutoff,
        )
        for row in candidates
    ])

    resolved_count = sum(r["resolved"] for r in market_results)
    inactive_count = sum(r["inactive"] for r in market_results)
    unknown_closed_count = sum(r["unknown_closed"] for r in market_results)
    skipped_count = sum(r["skipped"] for r in market_results)
    errors_count = sum(r["errors"] for r in market_results)
    fallback_clob = sum(r["fallback_clob"] for r in market_results)
    fallback_db = sum(r["fallback_db"] for r in market_results)
    positions_closed_count = sum(r["positions_closed"] for r in market_results)

    logger.info(
        "resolver_complete",
        resolved=resolved_count,
        marked_inactive=inactive_count,
        positions_closed=positions_closed_count,
        unknown_closed_markets=unknown_closed_count,
        skipped=skipped_count,
        errors=errors_count,
        fallback_clob=fallback_clob,
        fallback_db=fallback_db,
    )
    return {
        "resolved": resolved_count,
        "marked_inactive": inactive_count,
        "unknown_closed_markets": unknown_closed_count,
        "positions_closed": positions_closed_count,
        "skipped": skipped_count,
        "errors": errors_count,
        "fallback_clob": fallback_clob,
        "fallback_db": fallback_db,
    }


# ---------------------------------------------------------------------------
# Standalone entry point
# ---------------------------------------------------------------------------

async def main() -> None:
    setup_logging()
    started = datetime.now(tz=timezone.utc)
    print(f"=== Market Resolver — started {started.strftime('%Y-%m-%d %H:%M:%S UTC')} ===\n")

    result = await run_market_resolver()

    print("\n=== Resolution Summary ===")
    print(f"  Resolved            : {result['resolved']}")
    print(f"    via Gamma         : {result['resolved'] - result['fallback_clob'] - result['fallback_db']}")
    print(f"    via CLOB API      : {result['fallback_clob']}")
    print(f"    via DB prices     : {result['fallback_db']}")
    print(f"  Positions closed    : {result['positions_closed']}")
    print(f"  Unknown-outcome mkts: {result['unknown_closed_markets']}")
    print(f"  Marked inactive     : {result['marked_inactive']}")
    print(f"  Skipped             : {result['skipped']}")
    print(f"  Errors              : {result['errors']}")

    print("\nRunning retroactive snapshot backfill...")
    backfill = await backfill_retroactive_snapshots()
    print(f"  Snapshots created : {backfill['created']}")
    print(f"  Skipped           : {backfill['skipped']}")

    elapsed = (datetime.now(tz=timezone.utc) - started).total_seconds()
    print(f"\nTotal elapsed: {elapsed:.1f}s")


if __name__ == "__main__":
    if sys.platform == "win32":
        asyncio.run(main(), loop_factory=asyncio.SelectorEventLoop)
    else:
        asyncio.run(main())
