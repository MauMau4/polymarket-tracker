"""
Fix position_fraction and total_shares_at_time on SELL-sourced
wallet_closed_positions rows that were written by the backfill script
with incorrect hardcoded values (position_fraction=1.0,
total_shares_at_time=shares_sold).

Approach: for each unique (wallet, asset_id) with SELL WCP rows, replay
all trades in timestamp order to reconstruct the running share count, then
match each WCP row to the correct sell snapshot by closed_at timestamp.

Usage:
    python -m app.tasks.run_position_fraction_fix
"""
import asyncio
import sys
from datetime import datetime, timezone

from sqlalchemy import select

from app.db.models.trade import Trade
from app.db.models.wallet_closed_position import WalletClosedPosition
from app.db.session import get_session_factory
from app.logging import setup_logging, get_logger

logger = get_logger(__name__)

_MATCH_TOLERANCE_SECONDS = 5


async def fix_position_fractions() -> dict:
    """
    Replay trades per (wallet, asset_id) and update WCP rows with correct
    position_fraction and total_shares_at_time values.
    """
    factory = get_session_factory()

    async with factory() as session:
        pairs_result = await session.execute(
            select(WalletClosedPosition.wallet, WalletClosedPosition.asset_id)
            .where(WalletClosedPosition.is_resolved == False)  # noqa: E712
            .distinct()
        )
        pairs = pairs_result.all()

    print(f"Found {len(pairs)} (wallet, asset_id) pairs to process.")
    pairs_processed = 0
    rows_updated = 0
    rows_skipped = 0

    for wallet, asset_id in pairs:
        async with factory() as session:
            trades_result = await session.execute(
                select(Trade.side, Trade.size, Trade.ts)
                .where(
                    Trade.wallet == wallet,
                    Trade.asset_id == asset_id,
                    Trade.size.is_not(None),
                    Trade.trade_type.is_(None),
                )
                .order_by(Trade.ts.asc())
            )
            all_trades = trades_result.all()

            # Replay in order; build sell_snapshots: ts → (total_before, shares_sold)
            running_shares = 0.0
            sell_snapshots: dict[datetime, tuple[float, float]] = {}

            for row in all_trades:
                size = float(row.size or 0)
                side = (row.side or "").upper()
                if side == "BUY":
                    running_shares += size
                elif side == "SELL":
                    total_before = running_shares
                    running_shares = max(0.0, running_shares - size)
                    if row.ts not in sell_snapshots:
                        sell_snapshots[row.ts] = (total_before, size)

            if not sell_snapshots:
                pairs_processed += 1
                continue

            wcp_result = await session.execute(
                select(WalletClosedPosition)
                .where(
                    WalletClosedPosition.wallet == wallet,
                    WalletClosedPosition.asset_id == asset_id,
                    WalletClosedPosition.is_resolved == False,  # noqa: E712
                )
                .order_by(WalletClosedPosition.closed_at.asc())
            )
            wcp_rows = wcp_result.scalars().all()

            for wcp in wcp_rows:
                # Exact match first
                snapshot = sell_snapshots.get(wcp.closed_at)

                # Fall back to nearest within tolerance
                if snapshot is None:
                    best_delta = None
                    best_snap = None
                    for snap_ts, snap_val in sell_snapshots.items():
                        delta = abs((snap_ts - wcp.closed_at).total_seconds())
                        if delta <= _MATCH_TOLERANCE_SECONDS:
                            if best_delta is None or delta < best_delta:
                                best_delta = delta
                                best_snap = snap_val
                    snapshot = best_snap

                if snapshot is None:
                    logger.warning(
                        "position_fraction_fix_no_match",
                        wallet=wallet,
                        asset_id=asset_id,
                        closed_at=str(wcp.closed_at),
                    )
                    rows_skipped += 1
                    continue

                total_before, shares_sold = snapshot
                new_total = max(total_before, shares_sold)
                new_fraction = min(1.0, shares_sold / new_total) if new_total > 0 else 1.0

                wcp.position_fraction = new_fraction
                wcp.total_shares_at_time = new_total
                rows_updated += 1

            await session.commit()
        pairs_processed += 1

    return {
        "pairs_processed": pairs_processed,
        "rows_updated": rows_updated,
        "rows_skipped": rows_skipped,
    }


async def main() -> None:
    setup_logging()
    started = datetime.now(tz=timezone.utc)
    print(f"=== Position Fraction Fix — started {started.strftime('%Y-%m-%d %H:%M:%S UTC')} ===\n")

    result = await fix_position_fractions()

    print(f"\n=== Summary ===")
    print(f"  Pairs processed : {result['pairs_processed']}")
    print(f"  Rows updated    : {result['rows_updated']}")
    print(f"  Rows skipped    : {result['rows_skipped']}")

    if result["rows_updated"] > 0:
        print("\n[Rescoring] Running full wallet score recomputation...")
        try:
            from app.services.wallets.scorer import run_all_wallet_scores
            factory = get_session_factory()
            async with factory() as session:
                score_result = await run_all_wallet_scores(session)
                await session.commit()
            print(f"  Wallets scored: {score_result.get('wallets_scored', 0)}")
        except Exception as exc:
            print(f"  Scoring error: {exc}")

    elapsed = (datetime.now(tz=timezone.utc) - started).total_seconds()
    print(f"  Elapsed         : {elapsed:.1f}s")


if __name__ == "__main__":
    if sys.platform == "win32":
        asyncio.run(main(), loop_factory=asyncio.SelectorEventLoop)
    else:
        asyncio.run(main())
