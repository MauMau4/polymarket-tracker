"""wallet_stats_and_retention — aggregate skill stats, Welford state, outcome_result, sub-$20 cleanup

Revision ID: 0004
Revises: 0003
Create Date: 2026-04-26 00:00:00.000000
"""
from typing import Sequence, Union
import sqlalchemy as sa
from alembic import op

revision: str = "0004"
down_revision: Union[str, None] = "0003"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # ── Wallet aggregate stat fields ──────────────────────────────────────────
    op.add_column("wallets", sa.Column("avg_skill_score", sa.Float(), nullable=True))
    op.add_column("wallets", sa.Column("avg_edge_score", sa.Float(), nullable=True))
    op.add_column("wallets", sa.Column("skill_consistency", sa.Float(), nullable=True))
    op.add_column("wallets", sa.Column("win_rate", sa.Float(), nullable=True))
    op.add_column("wallets", sa.Column("realized_pnl", sa.Float(), nullable=True))
    op.add_column(
        "wallets",
        sa.Column("resolved_trades_count", sa.Integer(), nullable=False, server_default="0"),
    )
    op.add_column(
        "wallets",
        sa.Column("score_computed_at", sa.DateTime(timezone=True), nullable=True),
    )
    # Welford's algorithm running state (for incremental stdev without re-reading history)
    op.add_column("wallets", sa.Column("welford_skill_m2", sa.Float(), nullable=True))
    # Badge helper: set by scoring job when wallet has farming-tagged trades
    op.add_column(
        "wallets",
        sa.Column("has_farming_trades", sa.Boolean(), nullable=False, server_default="false"),
    )

    # ── Trade: outcome_result ─────────────────────────────────────────────────
    # 1 = winning side, 0 = losing side, NULL = market not yet resolved
    op.add_column("trades", sa.Column("outcome_result", sa.SmallInteger(), nullable=True))
    op.create_index("ix_trades_outcome_result", "trades", ["market_id", "outcome_result"])

    # ── One-time cleanup: delete sub-$20 trades ───────────────────────────────
    op.execute("DELETE FROM trades WHERE notional_usd IS NOT NULL AND notional_usd < 20")


def downgrade() -> None:
    op.drop_index("ix_trades_outcome_result", table_name="trades")
    op.drop_column("trades", "outcome_result")

    op.drop_column("wallets", "has_farming_trades")
    op.drop_column("wallets", "welford_skill_m2")
    op.drop_column("wallets", "score_computed_at")
    op.drop_column("wallets", "resolved_trades_count")
    op.drop_column("wallets", "realized_pnl")
    op.drop_column("wallets", "win_rate")
    op.drop_column("wallets", "skill_consistency")
    op.drop_column("wallets", "avg_edge_score")
    op.drop_column("wallets", "avg_skill_score")
