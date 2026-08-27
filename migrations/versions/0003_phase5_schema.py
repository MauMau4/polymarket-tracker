"""phase5_schema — skill scores, trade type, sybil flag

Revision ID: 0003
Revises: 0002
Create Date: 2026-04-26 00:00:00.000000
"""
from typing import Sequence, Union
import sqlalchemy as sa
from alembic import op

revision: str = "0003"
down_revision: Union[str, None] = "0002"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Per-trade skill and edge scores (computed when market resolves)
    op.add_column("trades", sa.Column("skill_score", sa.Numeric(10, 6), nullable=True))
    op.add_column("trades", sa.Column("edge_score", sa.Numeric(10, 6), nullable=True))
    # NULL = normal, 'farming' = filtered from scoring, 'unwind' = position exit
    op.add_column("trades", sa.Column("trade_type", sa.String(), nullable=True))

    # Sybil flag on wallets
    op.add_column(
        "wallets",
        sa.Column("suspected_sybil", sa.Boolean(), nullable=False, server_default="false"),
    )
    op.create_index("ix_wallets_suspected_sybil", "wallets", ["suspected_sybil"])

    # Index for efficient skill-score queries per wallet
    op.create_index("ix_trades_skill_score", "trades", ["wallet", "skill_score"])


def downgrade() -> None:
    op.drop_index("ix_trades_skill_score", table_name="trades")
    op.drop_index("ix_wallets_suspected_sybil", table_name="wallets")
    op.drop_column("wallets", "suspected_sybil")
    op.drop_column("trades", "trade_type")
    op.drop_column("trades", "edge_score")
    op.drop_column("trades", "skill_score")
