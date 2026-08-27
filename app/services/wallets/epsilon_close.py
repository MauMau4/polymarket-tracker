"""
Epsilon-close: closes residual sub-share dust left by the WS ingestor's
$20 minimum-notional filter (app/services/polymarket/normalizer.py — every
non-watched-wallet trade under trade_minimum_notional_usd is dropped, so
the final small sell that fully exits a position is often never observed).
Confirmed real (not float drift, not the 07-11 oversell cap) via on-chain
check — see decisions/2026-07-17.md.

Candidates: wallet_positions, status='Open', 0 < remaining_shares < 1.0,
market unresolved, no trade activity (either side) on (wallet, asset_id) in
the last _STALENESS_DAYS days. N=3 (reduced from the 2026-07-17 launch value
of 5): the only observed multi-day unwind gap in this data is +2 days (the
Ronaldo Ballon d'Or case), so 3 covers it with margin. closed_at is stamped
at the position's last observed trade — the actual economic exit moment —
not at job-run time, so N is purely an operational buffer against late-sell
accounting noise, not a data-accuracy parameter.

Valuation fallback chain, tier tracked per row and rolled up in the result:
  1. wallet's own last trade price for the asset (trades table)
  2. position's avg_entry_price
  3. most recent trade price for the asset from ANY wallet

Writes one WalletClosedPosition per candidate (is_resolved=False,
closed_at_source='residual_close'), then closes the wallet_positions row —
same shape as close_position_on_resolution. Duplicate-safe (skips any
(wallet, asset_id) with an existing residual_close WCP row).
"""
from datetime import datetime, timedelta, timezone

from sqlalchemy import select, func as sqlfunc

from app.db.models.market import Market
from app.db.models.trade import Trade
from app.db.models.wallet_closed_position import WalletClosedPosition
from app.db.models.wallet_position import WalletPosition
from app.db.session import get_session_factory
from app.logging import get_logger
from app.services.wallets.scorer import compute_log_roi

logger = get_logger(__name__)

_STALENESS_DAYS = 3


async def _find_candidates() -> list[tuple[str, str, datetime]]:
    """Returns (wallet, asset_id, last_activity) for every stale-dust position."""
    factory = get_session_factory()
    now = datetime.now(tz=timezone.utc)
    staleness_cutoff = now - timedelta(days=_STALENESS_DAYS)

    async with factory() as session:
        rows = (await session.execute(
            select(WalletPosition.wallet, WalletPosition.asset_id)
            .join(Market, Market.market_id == WalletPosition.market_id)
            .where(
                WalletPosition.status == "Open",
                WalletPosition.remaining_shares > 0,
                WalletPosition.remaining_shares < 1.0,
                Market.resolved == False,  # noqa: E712
            )
        )).all()

        candidates = []
        for wallet, asset_id in rows:
            last_activity = (await session.execute(
                select(sqlfunc.max(Trade.ts)).where(
                    Trade.wallet == wallet, Trade.asset_id == asset_id
                )
            )).scalar_one_or_none()
            if last_activity is None:
                # No observed trade history at all for this (wallet, asset_id) —
                # a different case (manually-seeded/backfilled position, same
                # pattern as pre-ingestion wallets) than "we saw it get built and
                # unwind below the observation floor." Out of scope here.
                continue
            if last_activity > staleness_cutoff:
                continue
            candidates.append((wallet, asset_id, last_activity))
    return candidates


async def _resolve_valuation(session, wallet: str, asset_id: str, avg_entry_price: float) -> tuple[float | None, str | None]:
    tier1 = (await session.execute(
        select(Trade.price)
        .where(Trade.wallet == wallet, Trade.asset_id == asset_id, Trade.price.is_not(None))
        .order_by(Trade.ts.desc())
        .limit(1)
    )).scalar_one_or_none()
    if tier1 is not None:
        return float(tier1), "wallet_last_trade"

    if avg_entry_price and avg_entry_price > 0:
        return avg_entry_price, "position_avg_entry_price"

    tier3 = (await session.execute(
        select(Trade.price)
        .where(Trade.asset_id == asset_id, Trade.price.is_not(None))
        .order_by(Trade.ts.desc())
        .limit(1)
    )).scalar_one_or_none()
    if tier3 is not None:
        return float(tier3), "any_wallet_last_print"

    return None, None


async def run_epsilon_close() -> dict:
    """Find and close eligible residual-dust positions. Returns summary counts
    (candidates, closed, skipped_already_closed, skipped_no_valuation, errors,
    valuation_tiers) suitable for scheduled-job logging."""
    factory = get_session_factory()

    candidates = await _find_candidates()

    closed = 0
    skipped_already_closed = 0
    skipped_no_valuation = 0
    errors = 0
    valuation_tiers: dict[str, int] = {}

    for wallet, asset_id, last_activity in candidates:
        try:
            async with factory() as session:
                dup = (await session.execute(
                    select(sqlfunc.count()).select_from(WalletClosedPosition).where(
                        WalletClosedPosition.wallet == wallet,
                        WalletClosedPosition.asset_id == asset_id,
                        WalletClosedPosition.closed_at_source == "residual_close",
                    )
                )).scalar_one()
                if dup > 0:
                    skipped_already_closed += 1
                    continue

                pos_result = await session.execute(
                    select(WalletPosition).where(
                        WalletPosition.wallet == wallet, WalletPosition.asset_id == asset_id
                    )
                )
                pos = pos_result.scalar_one_or_none()
                if pos is None or pos.status != "Open" or not (0 < pos.remaining_shares < 1.0):
                    continue  # state moved since candidate scan (e.g. new trade arrived)

                exit_price, tier = await _resolve_valuation(session, wallet, asset_id, pos.avg_entry_price)
                if exit_price is None:
                    skipped_no_valuation += 1
                    logger.warning("epsilon_close_no_valuation", wallet=wallet, asset_id=asset_id)
                    continue

                entry_price = pos.avg_entry_price
                shares = pos.remaining_shares
                # Economic exit moment = last observed trade on this asset, not job-run time.
                closed_at = last_activity or datetime.now(tz=timezone.utc)

                log_roi_val = compute_log_roi(exit_price, entry_price) or 0.0 if entry_price > 0 else 0.0
                cost_basis_val = entry_price * shares
                realized_pnl_val = (exit_price - entry_price) * shares if entry_price > 0 else 0.0

                lifetime_row = await session.execute(
                    select(sqlfunc.sum(Trade.notional_usd))
                    .where(Trade.wallet == wallet, Trade.notional_usd.is_not(None), Trade.side == "BUY")
                )
                lifetime_notional = float(lifetime_row.scalar_one_or_none() or 0.0)
                conviction = cost_basis_val / lifetime_notional if lifetime_notional > 0 else 0.0

                total_at_time = pos.total_shares_bought or shares
                position_fraction = shares / total_at_time if total_at_time > 0 else 1.0

                session.add(WalletClosedPosition(
                    wallet=wallet,
                    asset_id=asset_id,
                    market_id=pos.market_id,
                    entry_price=entry_price,
                    exit_price=exit_price,
                    shares_sold=shares,
                    total_shares_at_time=total_at_time,
                    position_fraction=position_fraction,
                    cost_basis=cost_basis_val,
                    conviction_weight=conviction,
                    log_roi=log_roi_val,
                    realized_pnl=realized_pnl_val,
                    is_resolved=False,
                    closed_at=closed_at,
                    closed_at_source="residual_close",
                ))

                pos.total_shares_sold += shares
                pos.remaining_shares = 0.0
                pos.realized_pnl += realized_pnl_val
                pos.status = "Closed"
                pos.closed_at = closed_at

                await session.commit()
                closed += 1
                valuation_tiers[tier] = valuation_tiers.get(tier, 0) + 1
                logger.debug(
                    "epsilon_closed",
                    wallet=wallet, asset_id=asset_id, shares=shares,
                    exit_price=exit_price, valuation_tier=tier, closed_at=str(closed_at),
                )
        except Exception as exc:
            errors += 1
            logger.error("epsilon_close_error", wallet=wallet, asset_id=asset_id, error=str(exc), exc_info=True)

    result = {
        "candidates": len(candidates),
        "closed": closed,
        "skipped_already_closed": skipped_already_closed,
        "skipped_no_valuation": skipped_no_valuation,
        "errors": errors,
        "valuation_tiers": valuation_tiers,
    }

    if closed > 0:
        from app.services.wallets.scorer import run_all_wallet_scores
        async with factory() as session:
            rescore_result = await run_all_wallet_scores(session)
            await session.commit()
        result["wallets_rescored"] = rescore_result.get("wallets_scored", 0)

    return result
