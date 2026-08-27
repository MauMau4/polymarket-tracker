"""Point-in-time wallet scoring engine (architecture §3.1, PRD §5.1, FR-1).

This module has exactly one job: given a wallet (or the whole universe) and
an `as_of` timestamp, decide qualification using ONLY closed positions with
`closed_at < as_of`. This is the single code path score_wallet/score_universe
share — and the only one the future daemon (M4+) is allowed to call.

Deliberate divergences from the tracker's existing (unrelated) wallet-scoring
code in app/services/wallets/scorer.py — logged in decisions/2026-07-15.md:

- ROI is computed from raw WalletClosedPosition fields
  (realized_pnl / cost_basis), NOT the pre-existing `log_roi` column. That
  column applies a ln(0.01) floor-clamp specific to the dashboard's own
  scoring system; the PRD's ROI definition (architecture §3.1) is plain
  arithmetic (exit_value - entry_cost) / entry_cost, which realized_pnl /
  cost_basis reproduces exactly (see docs/m1-schema-audit.md Q1).
- concentration_ratio = max(|pnl_i|) / sum(|pnl_i|) over the window's
  positions. The PRD doesn't specify a formula for "one position drives >60%
  of window PnL"; using the signed sum as denominator breaks under sign
  cancellation (a wallet with offsetting +/- positions can look arbitrarily
  "concentrated" even though no single position dominates). Sum of absolute
  values is stable and conservative.

Disputed/invalidated market exclusion (PRD R-6, architecture §3.1): Polymarket
exposes no direct dispute flag anywhere in this schema (docs/m1-schema-audit.md
Q4). What we actually have is `closed_at_source`, which the 2026-07-14 audit
used to tag every resolution-driven WCP row whose resolved status could not
be verified live against Gamma (`end_date_proxy`) — this includes the
false-resolution/drift cases the audit found (e.g. markets 630963, 2332744).
Filtering out `config.scoring.excluded_closed_at_sources` is the operative
mechanism for M1; it is NOT a general UMA-dispute filter (that data isn't
ingested at all yet — a real gap, not silently paved over here).

Sybil exclusion uses `wallets.suspected_sybil`, which is current-state only
(no history of when a wallet was flagged). This is an accepted limitation
documented in the schema audit, not a violation of the no-lookahead invariant
below — that invariant concerns price/outcome data, not the Sybil gate.

Invariant: no query in this module filters or joins on any column whose
value could reflect information that only became available after `as_of`
(other than the accepted Sybil-flag limitation above). See
tests/scoring/test_no_lookahead.py — permanent, must never be weakened.
"""
from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal

from sqlalchemy import or_, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models.wallet import Wallet
from app.db.models.wallet_closed_position import WalletClosedPosition
from app.db.models.wallet_score_pit import WalletScorePit
from pathfinder.config import PathfinderConfig, ScoringConfig
from pathfinder.scoring.models import WalletScore


@dataclass(frozen=True)
class _PositionRow:
    cost_basis: Decimal
    realized_pnl: Decimal
    entry_price: Decimal


def _to_decimal(value: float) -> Decimal:
    # str() round-trip avoids binary-float artifacts (e.g. Decimal(0.1) != 0.1).
    return Decimal(str(value))


def _closed_at_source_filter(stmt, excluded_sources: list[str]):
    """Rows with no source (legacy) are trusted by default; only rows
    explicitly tagged with an excluded source are dropped."""
    if not excluded_sources:
        return stmt
    return stmt.where(
        or_(
            WalletClosedPosition.closed_at_source.is_(None),
            WalletClosedPosition.closed_at_source.not_in(excluded_sources),
        )
    )


def _window_query(as_of: datetime, scoring: ScoringConfig):
    window_start = as_of - timedelta(days=scoring.formation_window_days)
    stmt = select(
        WalletClosedPosition.wallet,
        WalletClosedPosition.cost_basis,
        WalletClosedPosition.realized_pnl,
        WalletClosedPosition.entry_price,
    ).where(
        WalletClosedPosition.closed_at < as_of,
        WalletClosedPosition.closed_at >= window_start,
    )
    return _closed_at_source_filter(stmt, scoring.excluded_closed_at_sources)


def _qualify(
    wallet: str,
    as_of: datetime,
    config_version: str,
    rows: list[_PositionRow],
    scoring: ScoringConfig,
    suspected_sybil: bool,
) -> WalletScore:
    reasons: list[str] = []
    n_positions = len(rows)

    if scoring.sybil_exclusion_enabled and suspected_sybil:
        reasons.append("sybil_flagged")

    if n_positions < scoring.min_closed_positions:
        reasons.append("min_closed_positions")

    if n_positions == 0:
        return WalletScore(
            wallet=wallet,
            as_of=as_of,
            config_version=config_version,
            qualified=False,
            mean_roi=None,
            win_rate=None,
            implied_win_rate=None,
            n_positions=0,
            concentration_ratio=None,
            disqual_reasons=reasons,
        )

    # cost_basis <= 0 would be malformed data (the write paths in exit_skill.py
    # only ever record entry_price > 0 and shares_sold > 0, so cost_basis > 0
    # always holds in practice) — guarded so one bad row can't crash a whole
    # score_universe batch with a division error.
    rois = [r.realized_pnl / r.cost_basis for r in rows if r.cost_basis > 0]
    mean_roi = (sum(rois) / len(rois)) if rois else None

    wins = sum(1 for r in rows if r.realized_pnl > 0)
    win_rate = Decimal(wins) / Decimal(n_positions)
    implied_win_rate = sum((r.entry_price for r in rows), Decimal(0)) / Decimal(n_positions)

    abs_pnls = [abs(r.realized_pnl) for r in rows]
    total_abs_pnl = sum(abs_pnls, Decimal(0))
    concentration_ratio = (max(abs_pnls) / total_abs_pnl) if total_abs_pnl > 0 else Decimal(0)

    if mean_roi is None or mean_roi < _to_decimal(scoring.min_mean_roi):
        reasons.append("min_mean_roi")

    if scoring.skill_vs_implied_required and win_rate <= implied_win_rate:
        reasons.append("skill_vs_implied")

    if concentration_ratio > _to_decimal(scoring.concentration_cap):
        reasons.append("concentration_cap")

    return WalletScore(
        wallet=wallet,
        as_of=as_of,
        config_version=config_version,
        qualified=len(reasons) == 0,
        mean_roi=mean_roi,
        win_rate=win_rate,
        implied_win_rate=implied_win_rate,
        n_positions=n_positions,
        concentration_ratio=concentration_ratio,
        disqual_reasons=reasons,
    )


async def score_wallet(
    session: AsyncSession,
    wallet: str,
    as_of: datetime,
    config: PathfinderConfig,
) -> WalletScore:
    """Score a single wallet as-of a timestamp, using only positions with
    closed_at < as_of. Never reads current-state score/position tables."""
    stmt = _window_query(as_of, config.scoring).where(WalletClosedPosition.wallet == wallet)
    result = await session.execute(stmt)
    rows = [
        _PositionRow(
            cost_basis=_to_decimal(cost_basis),
            realized_pnl=_to_decimal(realized_pnl),
            entry_price=_to_decimal(entry_price),
        )
        for _wallet, cost_basis, realized_pnl, entry_price in result.all()
    ]

    sybil_result = await session.execute(
        select(Wallet.suspected_sybil).where(Wallet.wallet == wallet)
    )
    suspected_sybil = bool(sybil_result.scalar_one_or_none() or False)

    return _qualify(wallet, as_of, config.config_version, rows, config.scoring, suspected_sybil)


async def score_universe(
    session: AsyncSession,
    as_of: datetime,
    config: PathfinderConfig,
    materialize: bool = True,
) -> list[WalletScore]:
    """Score every wallet with at least one closed position in the formation
    window, in a single SQL pass. Wallets with zero positions in the window
    trivially fail min_closed_positions and are not materialized — scoring
    the full wallets table (including wallets that never traded) would only
    produce rows that are all identically unqualified.
    """
    stmt = _window_query(as_of, config.scoring)
    result = await session.execute(stmt)

    grouped: dict[str, list[_PositionRow]] = defaultdict(list)
    for wallet, cost_basis, realized_pnl, entry_price in result.all():
        grouped[wallet].append(
            _PositionRow(
                cost_basis=_to_decimal(cost_basis),
                realized_pnl=_to_decimal(realized_pnl),
                entry_price=_to_decimal(entry_price),
            )
        )

    if not grouped:
        return []

    sybil_result = await session.execute(
        select(Wallet.wallet, Wallet.suspected_sybil).where(Wallet.wallet.in_(grouped.keys()))
    )
    sybil_flags = {wallet: bool(flag) for wallet, flag in sybil_result.all()}

    scores = [
        _qualify(
            wallet,
            as_of,
            config.config_version,
            rows,
            config.scoring,
            sybil_flags.get(wallet, False),
        )
        for wallet, rows in grouped.items()
    ]

    if materialize:
        await materialize_scores(session, scores)

    return scores


async def materialize_scores(session: AsyncSession, scores: list[WalletScore]) -> None:
    """Upsert WalletScore results into wallet_scores_pit.

    Idempotent by (wallet, as_of, config_version): re-running score_universe
    for the same as_of/config overwrites rather than duplicating or erroring,
    which the determinism test relies on.
    """
    if not scores:
        return

    rows = [
        {
            "wallet": s.wallet,
            "as_of": s.as_of,
            "config_version": s.config_version,
            "qualified": s.qualified,
            "mean_roi": s.mean_roi,
            "win_rate": s.win_rate,
            "implied_win_rate": s.implied_win_rate,
            "n_positions": s.n_positions,
            "concentration_ratio": s.concentration_ratio,
            "disqual_reasons": s.disqual_reasons,
        }
        for s in scores
    ]

    stmt = pg_insert(WalletScorePit).values(rows)
    update_cols = {
        col: getattr(stmt.excluded, col)
        for col in (
            "qualified",
            "mean_roi",
            "win_rate",
            "implied_win_rate",
            "n_positions",
            "concentration_ratio",
            "disqual_reasons",
        )
    }
    stmt = stmt.on_conflict_do_update(
        index_elements=["wallet", "as_of", "config_version"],
        set_=update_cols,
    )
    await session.execute(stmt)
