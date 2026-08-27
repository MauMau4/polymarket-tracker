"""
Recovery for wallet_closed_positions rows lost when the first
stranded_positions_check run deleted wallet_cost_basis rows without writing
wallet_closed_positions (broad duplicate check was the bug).

After the buggy run, resolution.py's 30-min cycle ran and set outcome_result
on those BUY trades — so they no longer show up as outcome_result=NULL.
The WCB rows are gone; the BUY trades remain.

Recovery approach (WCB reconstruction):
  For each (wallet, asset_id, market_id) in a resolved market:
    net_shares = sum(all BUY trade sizes) - sum(SELL WCP shares for this wallet/asset/market)
    If net_shares > 0 and no WCP with shares ≈ net_shares exists (is_resolved=True):
      avg_entry_price = weighted avg of all BUY trade prices
      Write one WCP row for net_shares

This exactly replicates what the WCB row held at the time it was stranded.

Also sets outcome_result on any BUY trades (trade_type=NULL) that still have
it NULL, matching what resolution.py would have done.

Safe to run multiple times — uses shares-based duplicate check.

Usage:
    python -m app.tasks.run_late_buy_recovery
"""
import asyncio
import sys
from datetime import datetime, timezone

from sqlalchemy import select, func as sqlfunc

from app.db.models.market import Market
from app.db.models.token import Token
from app.db.models.trade import Trade
from app.db.models.wallet_closed_position import WalletClosedPosition
from app.db.session import get_session_factory
from app.services.wallets.scorer import compute_log_roi
from app.logging import setup_logging, get_logger

logger = get_logger(__name__)

_SHARES_TOLERANCE = 0.01  # wider tolerance for reconstructed values


async def run_late_buy_recovery() -> dict:
    """
    Reconstruct and write missing resolved WCP rows from BUY trade data.
    Returns summary dict.
    """
    now = datetime.now(tz=timezone.utc)
    factory = get_session_factory()

    # Step 1: find all (wallet, asset_id, market_id) groups where market is resolved
    # and at least one BUY trade exists (any trade_type, any outcome_result).
    async with factory() as session:
        groups_result = await session.execute(
            select(
                Trade.wallet,
                Trade.asset_id,
                Trade.market_id,
            )
            .join(Market, Market.market_id == Trade.market_id)
            .where(
                Trade.side == "BUY",
                Trade.wallet.is_not(None),
                Trade.price.is_not(None),
                Trade.size.is_not(None),
                Market.resolved == True,  # noqa: E712
                Market.resolution.is_not(None),
            )
            .distinct()
        )
        candidate_groups = groups_result.all()

    if not candidate_groups:
        logger.info("late_buy_recovery_none_found")
        return {"candidates": 0, "written": 0, "skipped_dup": 0, "skipped_bad_data": 0}

    logger.info("late_buy_recovery_candidates", count=len(candidate_groups))

    # Batch-load market and token data
    async with factory() as session:
        mkt_ids = list({g.market_id for g in candidate_groups})
        asset_ids = list({g.asset_id for g in candidate_groups})

        mkt_result = await session.execute(
            select(Market).where(Market.market_id.in_(mkt_ids))
        )
        markets = {m.market_id: m for m in mkt_result.scalars().all()}

        tok_result = await session.execute(
            select(Token.asset_id, Token.outcome).where(Token.asset_id.in_(asset_ids))
        )
        token_outcomes = {r.asset_id: r.outcome for r in tok_result.all()}

    written = 0
    skipped_dup = 0
    skipped_bad = 0

    for wallet, asset_id, market_id in candidate_groups:
        market = markets.get(market_id)
        if market is None or not market.resolved or not market.resolution:
            skipped_bad += 1
            continue

        token_outcome = token_outcomes.get(asset_id)
        if token_outcome is None:
            logger.warning(
                "late_buy_recovery_missing_token",
                wallet=wallet,
                asset_id=asset_id,
                market_id=market_id,
            )
            skipped_bad += 1
            continue

        try:
            async with factory() as session:
                # Sum all BUY trades for this group (all trade_types)
                buy_result = await session.execute(
                    select(Trade.price, Trade.size, Trade.id, Trade.outcome_result, Trade.trade_type)
                    .where(
                        Trade.wallet == wallet,
                        Trade.asset_id == asset_id,
                        Trade.market_id == market_id,
                        Trade.side == "BUY",
                        Trade.price.is_not(None),
                        Trade.size.is_not(None),
                    )
                )
                buy_trades = buy_result.all()
                if not buy_trades:
                    skipped_bad += 1
                    continue

                total_buy_size = sum(float(t.size) for t in buy_trades)
                weighted_price_sum = sum(float(t.price) * float(t.size) for t in buy_trades)
                if total_buy_size <= 0:
                    skipped_bad += 1
                    continue
                avg_entry_price = weighted_price_sum / total_buy_size

                # Sum SELL WCP shares already written for this group (is_resolved=False)
                sell_wcp_result = await session.execute(
                    select(sqlfunc.sum(WalletClosedPosition.shares_sold))
                    .where(
                        WalletClosedPosition.wallet == wallet,
                        WalletClosedPosition.asset_id == asset_id,
                        WalletClosedPosition.market_id == market_id,
                        WalletClosedPosition.is_resolved == False,  # noqa: E712
                    )
                )
                total_sell_wcp = float(sell_wcp_result.scalar_one_or_none() or 0.0)

                # Net = shares held to resolution
                net_shares = total_buy_size - total_sell_wcp
                if net_shares < 0.01:
                    skipped_bad += 1
                    continue

                # Shares-based duplicate check against existing resolved WCP rows
                dup_result = await session.execute(
                    select(sqlfunc.count())
                    .select_from(WalletClosedPosition)
                    .where(
                        WalletClosedPosition.wallet == wallet,
                        WalletClosedPosition.asset_id == asset_id,
                        WalletClosedPosition.market_id == market_id,
                        WalletClosedPosition.is_resolved == True,  # noqa: E712
                        sqlfunc.abs(WalletClosedPosition.shares_sold - net_shares) < _SHARES_TOLERANCE,
                    )
                )
                if dup_result.scalar_one() > 0:
                    skipped_dup += 1
                    continue

                resolution_str = market.resolution.upper()
                outcome_str = (token_outcome or "").upper()
                exit_price = 1.0 if outcome_str == resolution_str else 0.0

                log_roi_val = compute_log_roi(exit_price, avg_entry_price) or 0.0
                cost_basis_val = avg_entry_price * net_shares
                realized_pnl_val = (exit_price - avg_entry_price) * net_shares

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
                    entry_price=avg_entry_price,
                    exit_price=exit_price,
                    shares_sold=net_shares,
                    total_shares_at_time=net_shares,
                    position_fraction=1.0,
                    cost_basis=cost_basis_val,
                    conviction_weight=conviction,
                    log_roi=log_roi_val,
                    realized_pnl=realized_pnl_val,
                    is_resolved=True,
                    closed_at=closed_at,
                ))

                # Set outcome_result on trade_type=NULL BUY trades that still have it NULL
                for t in buy_trades:
                    if t.outcome_result is None and t.trade_type is None:
                        trade_result = await session.execute(
                            select(Trade).where(Trade.id == t.id)
                        )
                        trade_obj = trade_result.scalar_one_or_none()
                        if trade_obj:
                            trade_outcome_str = (trade_obj.outcome or "").upper()
                            trade_obj.outcome_result = 1 if trade_outcome_str == resolution_str else 0

                await session.commit()

                logger.info(
                    "late_buy_recovery_written",
                    wallet=wallet,
                    asset_id=asset_id,
                    market_id=market_id,
                    net_shares=net_shares,
                    avg_entry=avg_entry_price,
                    exit_price=exit_price,
                    realized_pnl=realized_pnl_val,
                )
                written += 1

        except Exception as exc:
            logger.error(
                "late_buy_recovery_error",
                wallet=wallet,
                asset_id=asset_id,
                market_id=market_id,
                error=str(exc),
                exc_info=True,
            )
            skipped_bad += 1

    logger.info(
        "late_buy_recovery_complete",
        written=written,
        skipped_dup=skipped_dup,
        skipped_bad_data=skipped_bad,
    )

    wallets_scored = 0
    if written > 0:
        try:
            from app.services.wallets.scorer import run_all_wallet_scores
            async with factory() as session:
                score_result = await run_all_wallet_scores(session)
                await session.commit()
            wallets_scored = score_result.get("wallets_scored", 0)
            logger.info("late_buy_recovery_rescore_complete", wallets_scored=wallets_scored)
        except Exception as exc:
            logger.error("late_buy_recovery_rescore_error", error=str(exc), exc_info=True)

    return {
        "candidates": len(candidate_groups),
        "written": written,
        "skipped_dup": skipped_dup,
        "skipped_bad_data": skipped_bad,
        "wallets_rescored": wallets_scored,
    }


async def main() -> None:
    setup_logging()
    started = datetime.now(tz=timezone.utc)
    print(f"=== Late BUY Recovery — started {started.strftime('%Y-%m-%d %H:%M:%S UTC')} ===\n")

    result = await run_late_buy_recovery()

    print(f"\n=== Summary ===")
    print(f"  Candidate groups      : {result['candidates']}")
    print(f"  WCP rows written      : {result['written']}")
    print(f"  Skipped (duplicate)   : {result['skipped_dup']}")
    print(f"  Skipped (bad data)    : {result['skipped_bad_data']}")
    print(f"  Wallets rescored      : {result.get('wallets_rescored', 0)}")

    elapsed = (datetime.now(tz=timezone.utc) - started).total_seconds()
    print(f"  Elapsed               : {elapsed:.1f}s")


if __name__ == "__main__":
    if sys.platform == "win32":
        asyncio.run(main(), loop_factory=asyncio.SelectorEventLoop)
    else:
        asyncio.run(main())
