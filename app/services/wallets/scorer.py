"""
Wallet scoring — ROI-based system.

All scoring is derived from wallet_closed_positions (append-only table).

Score components (sourced exclusively from wallet_closed_positions):
  60% — Weighted Log ROI
  25% — Consistency  (1 / (1 + stdev(log_roi)))
  15% — Notional-weighted Win Rate

Components are min-max normalized across all qualifying wallets before
combining into a composite score.

Tier assignment (based on number of closed-position rows):
  < 10 rows:   "Unranked"
  10–25 rows:  "Developing"
  26–50 rows:  "Established"
  > 50 rows:   "Verified"

Cold-start rule: fewer than 10 rows → composite = 0.0, tier = "Unranked".
"""
import math
import statistics
from datetime import datetime, timedelta, timezone
from typing import NamedTuple

from sqlalchemy import case as sa_case, delete, func as sqlfunc, select, update as sqla_update
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models.market import Market
from app.db.models.trade import Trade
from app.db.models.wallet import Wallet
from app.db.models.wallet_closed_position import WalletClosedPosition
from app.db.models.wallet_score import WalletScoreHistory
from app.logging import get_logger

logger = get_logger(__name__)

_MIN_CLOSED_POSITIONS = 10

# Farming classification constants (unchanged from previous system)
_FARMING_PRICE_CEILING = 0.90
_FARMING_DAYS_CEILING = 60


# ---------------------------------------------------------------------------
# Pure math helpers
# ---------------------------------------------------------------------------

# Floor for the ROI ratio inside the log. The old floor of 1e-10 made a single
# total loss worth log_roi ≈ -23 vs ≈ +0.7 for a typical win, so one position
# held to a losing resolution dominated a wallet's weighted log ROI and stdev —
# systematically burying exactly the longshot-taking wallets we want to rank.
# 0.01 caps a total loss at log(0.01) ≈ -4.6.
_ROI_RATIO_FLOOR = 0.01
LOG_ROI_MIN = math.log(_ROI_RATIO_FLOOR)


def compute_log_roi(exit_price: float, entry_price: float) -> float | None:
    """log(1 + (exit - entry) / entry). Returns None for invalid inputs.
    Total-loss positions (exit=0) are clamped to log(0.01) ≈ -4.6."""
    if entry_price <= 0:
        return None
    ratio = (exit_price - entry_price) / entry_price
    argument = max(_ROI_RATIO_FLOOR, 1.0 + ratio)
    return math.log(argument)


def _assign_tier(closed_count: int) -> str:
    if closed_count < 10:
        return "Unranked"
    if closed_count <= 25:
        return "Developing"
    if closed_count <= 50:
        return "Established"
    return "Verified"


def _min_max_normalize(values: list[float]) -> list[float]:
    """Scale values to [0, 100]. All-equal → 50.0 for all."""
    if not values:
        return []
    vmin, vmax = min(values), max(values)
    if vmin == vmax:
        return [50.0] * len(values)
    span = vmax - vmin
    return [100.0 * (v - vmin) / span for v in values]


# ---------------------------------------------------------------------------
# Per-wallet raw metrics (before normalization)
# ---------------------------------------------------------------------------

class _WalletMetrics(NamedTuple):
    wallet: str
    closed_count: int
    weighted_log_roi: float
    consistency: float
    win_rate: float
    realized_pnl: float


def _compute_raw_metrics(wallet: str, positions: list) -> _WalletMetrics | None:
    """
    Compute unnormalized metrics for one wallet from its closed positions.
    Returns None if fewer than _MIN_CLOSED_POSITIONS rows.
    """
    if len(positions) < _MIN_CLOSED_POSITIONS:
        return None

    log_roi_values = [p.log_roi for p in positions]

    # Weighted Log ROI: Σ(log_roi * conviction * fraction) / Σ(conviction * fraction)
    weight_sum = sum(p.conviction_weight * p.position_fraction for p in positions)
    if weight_sum > 0:
        weighted_lr = sum(
            p.log_roi * p.conviction_weight * p.position_fraction for p in positions
        ) / weight_sum
    else:
        weighted_lr = float(statistics.mean(log_roi_values))

    # Consistency: 1 / (1 + stdev(log_roi))
    if len(log_roi_values) >= 2:
        stdev = statistics.stdev(log_roi_values)
        consistency = 1.0 / (1.0 + stdev)
    else:
        consistency = 0.0

    # Count-based win rate: each closed position is one decision
    win_rate = sum(1 for p in positions if p.log_roi > 0) / len(positions)

    # Realized PnL = Σ (exit_price - entry_price) * shares_sold
    realized_pnl = sum(
        (p.exit_price - p.entry_price) * p.shares_sold for p in positions
    )

    return _WalletMetrics(
        wallet=wallet,
        closed_count=len(positions),
        weighted_log_roi=weighted_lr,
        consistency=consistency,
        win_rate=win_rate,
        realized_pnl=realized_pnl,
    )


# ---------------------------------------------------------------------------
# Main scoring entry point
# ---------------------------------------------------------------------------

async def run_all_wallet_scores(session: AsyncSession) -> dict:
    """
    Full recomputation of wallet scores from wallet_closed_positions.

    Min-max normalization requires all wallets' raw metrics simultaneously,
    so this always processes every wallet in a single pass. Aggregation runs
    in SQL — the WCP table is append-only and loading every row into ORM
    objects grew without bound (300k+ rows).
    Caller must commit after returning.
    """
    agg_rows = (await session.execute(
        select(
            WalletClosedPosition.wallet,
            sqlfunc.count().label("closed_count"),
            sqlfunc.sum(
                WalletClosedPosition.log_roi
                * WalletClosedPosition.conviction_weight
                * WalletClosedPosition.position_fraction
            ).label("weighted_lr_num"),
            sqlfunc.sum(
                WalletClosedPosition.conviction_weight
                * WalletClosedPosition.position_fraction
            ).label("weight_sum"),
            sqlfunc.avg(WalletClosedPosition.log_roi).label("mean_lr"),
            sqlfunc.stddev_samp(WalletClosedPosition.log_roi).label("stdev_lr"),
            sqlfunc.sum(
                sa_case((WalletClosedPosition.log_roi > 0, 1), else_=0)
            ).label("wins"),
            sqlfunc.sum(
                (WalletClosedPosition.exit_price - WalletClosedPosition.entry_price)
                * WalletClosedPosition.shares_sold
            ).label("realized_pnl"),
        ).group_by(WalletClosedPosition.wallet)
    )).all()

    metrics_list: list[_WalletMetrics] = []
    for row in agg_rows:
        if row.closed_count < _MIN_CLOSED_POSITIONS:
            continue
        weight_sum = float(row.weight_sum or 0.0)
        if weight_sum > 0:
            weighted_lr = float(row.weighted_lr_num or 0.0) / weight_sum
        else:
            weighted_lr = float(row.mean_lr or 0.0)
        stdev = float(row.stdev_lr) if row.stdev_lr is not None else None
        consistency = 1.0 / (1.0 + stdev) if row.closed_count >= 2 and stdev is not None else 0.0
        metrics_list.append(_WalletMetrics(
            wallet=row.wallet,
            closed_count=row.closed_count,
            weighted_log_roi=weighted_lr,
            consistency=consistency,
            win_rate=row.wins / row.closed_count,
            realized_pnl=float(row.realized_pnl or 0.0),
        ))

    now_utc = datetime.now(tz=timezone.utc)
    today_start = now_utc.replace(hour=0, minute=0, second=0, microsecond=0)

    if not metrics_list:
        logger.info("scorer_no_qualifying_wallets")
        return {"wallets_scored": 0}

    # Normalize each component across qualifying wallets
    lr_norm = _min_max_normalize([m.weighted_log_roi for m in metrics_list])
    cons_norm = _min_max_normalize([m.consistency for m in metrics_list])
    wr_norm = _min_max_normalize([m.win_rate for m in metrics_list])

    # Batch-load only the qualifying wallets' ORM objects
    wallets_result = await session.execute(
        select(Wallet).where(Wallet.wallet.in_([m.wallet for m in metrics_list]))
    )
    wallets_dict: dict[str, Wallet] = {
        w.wallet: w for w in wallets_result.scalars().all()
    }

    wallets_scored = 0

    for i, m in enumerate(metrics_list):
        composite = (
            lr_norm[i] * 0.60
            + cons_norm[i] * 0.25
            + wr_norm[i] * 0.15
        )
        composite = max(0.0, min(100.0, composite))
        tier = _assign_tier(m.closed_count)

        wallet = wallets_dict.get(m.wallet)
        if wallet is None:
            continue

        wallet.wallet_score = composite
        wallet.score_confidence = tier
        wallet.win_rate = m.win_rate
        wallet.realized_pnl = m.realized_pnl
        wallet.resolved_trades_count = m.closed_count
        wallet.score_computed_at = now_utc

        # One score history entry per calendar day
        await session.execute(
            delete(WalletScoreHistory).where(
                WalletScoreHistory.wallet == m.wallet,
                WalletScoreHistory.snapshot_ts >= today_start,
                WalletScoreHistory.snapshot_ts < today_start + timedelta(days=1),
            )
        )
        session.add(WalletScoreHistory(
            wallet=m.wallet,
            score=composite,
            confidence=tier,
            components={
                "weighted_log_roi": round(m.weighted_log_roi, 6),
                "weighted_log_roi_normalized": round(lr_norm[i], 2),
                "consistency": round(m.consistency, 6),
                "consistency_normalized": round(cons_norm[i], 2),
                "win_rate": round(m.win_rate, 4),
                "win_rate_normalized": round(wr_norm[i], 2),
                "closed_count": m.closed_count,
            },
            snapshot_ts=now_utc,
        ))
        wallets_scored += 1

    # Zero out non-qualifying wallets whose scores may be stale (bulk update)
    qualifying_set = {m.wallet for m in metrics_list}
    await session.execute(
        sqla_update(Wallet)
        .where(
            Wallet.wallet_score != 0.0,
            Wallet.wallet.notin_(qualifying_set),
        )
        .values(wallet_score=0.0, score_confidence="Unranked")
        .execution_options(synchronize_session=False)
    )

    logger.info("scorer_complete", wallets_scored=wallets_scored)
    return {"wallets_scored": wallets_scored}


async def refresh_wallet_score(
    session: AsyncSession,
    wallet_address: str,
) -> tuple[float, str]:
    """
    Trigger a full recomputation of all wallet scores (normalization requires
    a global pass) and return the score for the requested wallet.
    Caller must commit.
    """
    await run_all_wallet_scores(session)

    wallet_result = await session.execute(
        select(Wallet).where(Wallet.wallet == wallet_address)
    )
    wallet = wallet_result.scalar_one_or_none()
    if wallet:
        return wallet.wallet_score, wallet.score_confidence
    return 0.0, "Unranked"


# ---------------------------------------------------------------------------
# Farming trade tagging (unchanged — feeds FARMER badge, not scoring)
# ---------------------------------------------------------------------------

def _is_farming(price: float | None, days_to_resolution: float | None) -> bool:
    """Both price ceiling AND days ceiling must be exceeded."""
    return (
        price is not None and price > _FARMING_PRICE_CEILING
        and days_to_resolution is not None and days_to_resolution > _FARMING_DAYS_CEILING
    )


async def tag_farming_trades(session: AsyncSession, wallet_address: str) -> int:
    """
    Tag trades as 'farming' where both the price and days-to-resolution
    criteria are met. Returns count of trades tagged. Caller must commit.
    """
    rows = await session.execute(
        select(Trade.id, Trade.price, Trade.ts, Trade.market_id)
        .join(Market, Market.market_id == Trade.market_id)
        .where(
            Trade.wallet == wallet_address,
            Trade.side == "BUY",
            Trade.trade_type.is_(None),
        )
    )
    trades = rows.all()

    market_ids = {r.market_id for r in trades}
    if not market_ids:
        return 0

    mrows = await session.execute(
        select(Market.market_id, Market.end_date)
        .where(Market.market_id.in_(list(market_ids)))
    )
    market_end = {r.market_id: r.end_date for r in mrows.all()}

    tagged = 0
    for row in trades:
        end_date = market_end.get(row.market_id)
        days_to_res = (end_date - row.ts).days if end_date and row.ts else None
        if _is_farming(float(row.price) if row.price else None, days_to_res):
            trade_result = await session.execute(select(Trade).where(Trade.id == row.id))
            trade = trade_result.scalar_one_or_none()
            if trade:
                trade.trade_type = "farming"
                tagged += 1

    return tagged
