"""
Stranded wallet_cost_basis cleanup.

Finds wallet_cost_basis rows for already-resolved markets and closes them
out: writes wallet_closed_positions rows and deletes the stranded rows.

This happens when a BUY trade is attributed after the resolution job has
already processed a market.  The ingestion path (exit_skill.py) now handles
this in real time; this scheduled job is the safety net for any that slip
through.

Safe to run multiple times — duplicate-checks before every write.

Schedule: every 6 hours via APScheduler (run_worker.py).
One-time backfill: python -m app.tasks.run_stranded_positions_check
"""
import asyncio
import sys
from datetime import datetime, timezone

from sqlalchemy import select, func as sqlfunc

from app.db.models.market import Market
from app.db.models.token import Token
from app.db.models.trade import Trade
from app.db.models.wallet_closed_position import WalletClosedPosition
from app.db.models.wallet_cost_basis import WalletCostBasis
from app.db.models.wallet_position import WalletPosition
from app.db.session import get_session_factory
from app.services.wallets.scorer import compute_log_roi
from app.logging import setup_logging, get_logger

logger = get_logger(__name__)


async def run_stranded_positions_check() -> dict:
    """
    Find and close all wallet_cost_basis rows whose market is already resolved.
    Returns a summary dict with counts.
    """
    now = datetime.now(tz=timezone.utc)
    factory = get_session_factory()

    # Load identifiers of all stranded rows in one query
    async with factory() as session:
        id_result = await session.execute(
            select(
                WalletCostBasis.wallet,
                WalletCostBasis.asset_id,
                WalletCostBasis.market_id,
            )
            .join(Market, Market.market_id == WalletCostBasis.market_id)
            .where(Market.resolved == True)  # noqa: E712
        )
        stranded_ids = id_result.all()

    if not stranded_ids:
        logger.info("stranded_positions_check_none_found")

    logger.info("stranded_positions_check_found", count=len(stranded_ids))

    written = 0
    skipped_dup = 0
    skipped_bad = 0

    for wallet, asset_id, market_id in stranded_ids:
        try:
            async with factory() as session:
                # Load all needed data fresh in this session
                pos_result = await session.execute(
                    select(WalletCostBasis).where(
                        WalletCostBasis.wallet == wallet,
                        WalletCostBasis.asset_id == asset_id,
                    )
                )
                pos = pos_result.scalar_one_or_none()
                if pos is None:
                    continue

                if pos.avg_entry_price <= 0 or pos.total_size <= 0:
                    await session.delete(pos)
                    await session.commit()
                    skipped_bad += 1
                    continue

                mkt_result = await session.execute(
                    select(Market).where(Market.market_id == market_id)
                )
                market = mkt_result.scalar_one_or_none()
                if market is None or not market.resolved or not market.resolution:
                    skipped_bad += 1
                    continue

                tok_result = await session.execute(
                    select(Token.outcome).where(Token.asset_id == asset_id)
                )
                token_outcome = tok_result.scalar_one_or_none()
                if token_outcome is None:
                    logger.warning(
                        "stranded_missing_token_outcome",
                        wallet=wallet,
                        asset_id=asset_id,
                        market_id=market_id,
                    )
                    skipped_bad += 1
                    continue

                # Duplicate guard: only skip if an existing resolved WCP row has
                # the same share count (±0.001).  A wallet can have multiple resolved
                # closure events on the same asset with different share amounts.
                dup_result = await session.execute(
                    select(sqlfunc.count())
                    .select_from(WalletClosedPosition)
                    .where(
                        WalletClosedPosition.wallet == wallet,
                        WalletClosedPosition.asset_id == asset_id,
                        WalletClosedPosition.market_id == market_id,
                        WalletClosedPosition.is_resolved == True,  # noqa: E712
                        sqlfunc.abs(WalletClosedPosition.shares_sold - pos.total_size) < 0.001,
                    )
                )
                if dup_result.scalar_one() > 0:
                    await session.delete(pos)
                    await session.commit()
                    skipped_dup += 1
                    continue

                resolution_str = market.resolution.upper()
                outcome_str = (token_outcome or "").upper()
                exit_price = 1.0 if outcome_str == resolution_str else 0.0

                log_roi_val = compute_log_roi(exit_price, pos.avg_entry_price) or 0.0
                cost_basis_val = pos.avg_entry_price * pos.total_size
                realized_pnl_val = (exit_price - pos.avg_entry_price) * pos.total_size

                lifetime_row = await session.execute(
                    select(sqlfunc.sum(Trade.notional_usd))
                    .where(
                        Trade.wallet == wallet,
                        Trade.notional_usd.is_not(None),
                        Trade.side == "BUY",
                    )
                )
                lifetime_notional = float(lifetime_row.scalar_one_or_none() or 0.0)
                conviction = cost_basis_val / lifetime_notional if lifetime_notional > 0 else 0.0

                closed_at = market.end_date or market.updated_at or now

                session.add(WalletClosedPosition(
                    wallet=wallet,
                    asset_id=asset_id,
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
                    closed_at=closed_at,
                ))
                await session.delete(pos)
                await session.commit()

                logger.info(
                    "stranded_position_closed",
                    wallet=wallet,
                    asset_id=asset_id,
                    market_id=market_id,
                    exit_price=exit_price,
                    shares=pos.total_size,
                    realized_pnl=realized_pnl_val,
                )
                written += 1

        except Exception as exc:
            logger.error(
                "stranded_close_error",
                wallet=wallet,
                asset_id=asset_id,
                market_id=market_id,
                error=str(exc),
                exc_info=True,
            )
            skipped_bad += 1

    logger.info(
        "stranded_positions_check_complete",
        written=written,
        skipped_dup=skipped_dup,
        skipped_bad_data=skipped_bad,
    )

    # ── Pass 2: wallet_positions orphan fix ───────────────────────────────────
    # Find wallet_positions rows that are still Open on resolved markets but have
    # no wallet_cost_basis row (already consumed). Pass 1 misses these entirely.
    orphans_found = 0
    orphans_closed = 0
    orphans_skipped = 0

    async with factory() as session:
        orphan_result = await session.execute(
            select(
                WalletPosition.wallet,
                WalletPosition.asset_id,
                WalletPosition.market_id,
            )
            .join(Market, Market.market_id == WalletPosition.market_id)
            .where(
                WalletPosition.status == "Open",
                Market.resolved == True,  # noqa: E712
            )
        )
        orphan_ids = orphan_result.all()

    orphans_found = len(orphan_ids)
    logger.info("stranded_orphans_found", count=orphans_found)

    for wallet, asset_id, market_id in orphan_ids:
        try:
            async with factory() as session:
                wp_result = await session.execute(
                    select(WalletPosition).where(
                        WalletPosition.wallet == wallet,
                        WalletPosition.asset_id == asset_id,
                    )
                )
                wp = wp_result.scalar_one_or_none()
                if wp is None or (wp.remaining_shares <= 0 and wp.status == "Closed"):
                    continue

                mkt_result = await session.execute(
                    select(Market).where(Market.market_id == market_id)
                )
                market = mkt_result.scalar_one_or_none()
                if market is None or not market.resolved or not market.resolution:
                    orphans_skipped += 1
                    continue

                tok_result = await session.execute(
                    select(Token.outcome).where(Token.asset_id == asset_id)
                )
                token_outcome = tok_result.scalar_one_or_none()
                if token_outcome is None:
                    logger.warning(
                        "orphan_missing_token_outcome",
                        wallet=wallet,
                        asset_id=asset_id,
                        market_id=market_id,
                    )
                    orphans_skipped += 1
                    continue

                resolution_str = market.resolution.upper()
                outcome_str = (token_outcome or "").upper()
                exit_price = 1.0 if outcome_str == resolution_str else 0.0
                closed_at = market.updated_at or market.end_date or now

                # Check for an existing resolved WCP row for this wallet/asset
                existing_wcp = await session.execute(
                    select(sqlfunc.count())
                    .select_from(WalletClosedPosition)
                    .where(
                        WalletClosedPosition.wallet == wallet,
                        WalletClosedPosition.asset_id == asset_id,
                        WalletClosedPosition.is_resolved == True,  # noqa: E712
                    )
                )
                has_wcp = existing_wcp.scalar_one() > 0

                if not has_wcp:
                    # No WCP exists — write one from wallet_positions data
                    shares = max(0.0, wp.remaining_shares)
                    if shares <= 0:
                        # No remaining shares; nothing to close, just mark status
                        pass
                    else:
                        bought = wp.total_shares_bought or shares
                        entry_price = (wp.total_cost_basis / bought) if bought > 0 else 0.0
                        if entry_price > 0:
                            log_roi_val = compute_log_roi(exit_price, entry_price) or 0.0
                            cost_basis_val = entry_price * shares
                            realized_pnl_val = (exit_price - entry_price) * shares

                            lifetime_row = await session.execute(
                                select(sqlfunc.sum(Trade.notional_usd))
                                .where(
                                    Trade.wallet == wallet,
                                    Trade.notional_usd.is_not(None),
                                    Trade.side == "BUY",
                                )
                            )
                            lifetime_notional = float(lifetime_row.scalar_one_or_none() or 0.0)
                            conviction = cost_basis_val / lifetime_notional if lifetime_notional > 0 else 0.0

                            session.add(WalletClosedPosition(
                                wallet=wallet,
                                asset_id=asset_id,
                                market_id=market_id,
                                entry_price=entry_price,
                                exit_price=exit_price,
                                shares_sold=shares,
                                total_shares_at_time=shares,
                                position_fraction=1.0,
                                cost_basis=cost_basis_val,
                                conviction_weight=conviction,
                                log_roi=log_roi_val,
                                realized_pnl=realized_pnl_val,
                                is_resolved=True,
                                closed_at=closed_at,
                            ))
                            logger.info(
                                "orphan_wcp_written",
                                wallet=wallet,
                                asset_id=asset_id,
                                market_id=market_id,
                                exit_price=exit_price,
                                shares=shares,
                                realized_pnl=realized_pnl_val,
                            )

                # Recalculate realized_pnl from all WCP rows for this wallet/asset
                wcp_pnl_result = await session.execute(
                    select(sqlfunc.sum(WalletClosedPosition.realized_pnl))
                    .where(
                        WalletClosedPosition.wallet == wallet,
                        WalletClosedPosition.asset_id == asset_id,
                    )
                )
                wcp_realized_pnl = float(wcp_pnl_result.scalar_one_or_none() or 0.0)

                wp.status = "Closed"
                wp.remaining_shares = 0.0
                wp.closed_at = closed_at
                wp.realized_pnl = wcp_realized_pnl

                await session.commit()
                orphans_closed += 1

        except Exception as exc:
            logger.error(
                "orphan_close_error",
                wallet=wallet,
                asset_id=asset_id,
                market_id=market_id,
                error=str(exc),
                exc_info=True,
            )
            orphans_skipped += 1

    logger.info(
        "stranded_orphans_complete",
        found=orphans_found,
        closed=orphans_closed,
        skipped=orphans_skipped,
    )

    any_written = written > 0 or orphans_closed > 0
    if any_written:
        try:
            from app.services.wallets.scorer import run_all_wallet_scores
            async with factory() as session:
                score_result = await run_all_wallet_scores(session)
                await session.commit()
            wallets_scored = score_result.get("wallets_scored", 0)
            logger.info("stranded_rescore_complete", wallets_scored=wallets_scored)
        except Exception as exc:
            logger.error("stranded_rescore_error", error=str(exc), exc_info=True)
            wallets_scored = 0
    else:
        wallets_scored = 0

    return {
        "checked": len(stranded_ids),
        "written": written,
        "skipped_dup": skipped_dup,
        "skipped_bad_data": skipped_bad,
        "orphans_found": orphans_found,
        "orphans_closed": orphans_closed,
        "orphans_skipped": orphans_skipped,
        "wallets_rescored": wallets_scored,
    }


async def main() -> None:
    setup_logging()
    started = datetime.now(tz=timezone.utc)
    print(f"=== Stranded Positions Check — started {started.strftime('%Y-%m-%d %H:%M:%S UTC')} ===\n")

    result = await run_stranded_positions_check()

    print(f"\n=== Summary ===")
    print(f"  Pass 1 — wallet_cost_basis")
    print(f"    Stranded rows checked : {result['checked']}")
    print(f"    WCP rows written      : {result['written']}")
    print(f"    Skipped (duplicate)   : {result['skipped_dup']}")
    print(f"    Skipped (bad data)    : {result['skipped_bad_data']}")
    print(f"  Pass 2 — wallet_positions orphans")
    print(f"    Orphans found         : {result['orphans_found']}")
    print(f"    Orphans closed        : {result['orphans_closed']}")
    print(f"    Orphans skipped       : {result['orphans_skipped']}")
    print(f"  Wallets rescored        : {result.get('wallets_rescored', 0)}")

    elapsed = (datetime.now(tz=timezone.utc) - started).total_seconds()
    print(f"  Elapsed               : {elapsed:.1f}s")


if __name__ == "__main__":
    if sys.platform == "win32":
        asyncio.run(main(), loop_factory=asyncio.SelectorEventLoop)
    else:
        asyncio.run(main())
