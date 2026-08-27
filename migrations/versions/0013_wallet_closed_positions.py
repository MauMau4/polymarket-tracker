"""wallet_closed_positions

Add append-only table for closed position events (sells + resolution).
Feeds the new ROI-based wallet scoring system.

Revision ID: 0013
Revises: 0012
Create Date: 2026-05-05 00:00:00.000000
"""
from typing import Sequence, Union
import sqlalchemy as sa
from alembic import op

revision: str = "0013"
down_revision: Union[str, None] = "0012"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "wallet_closed_positions",
        sa.Column("id", sa.String, primary_key=True),
        sa.Column("wallet", sa.String, nullable=False),
        sa.Column("asset_id", sa.String, nullable=False),
        sa.Column("market_id", sa.String, nullable=False),
        sa.Column("entry_price", sa.Float, nullable=False),
        sa.Column("exit_price", sa.Float, nullable=False),
        sa.Column("shares_sold", sa.Float, nullable=False),
        sa.Column("total_shares_at_time", sa.Float, nullable=False),
        sa.Column("position_fraction", sa.Float, nullable=False),
        sa.Column("notional_usd", sa.Float, nullable=False),
        sa.Column("conviction_weight", sa.Float, nullable=False),
        sa.Column("log_roi", sa.Float, nullable=False),
        sa.Column("is_resolved", sa.Boolean, nullable=False),
        sa.Column("closed_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index(
        "ix_wallet_closed_positions_wallet",
        "wallet_closed_positions",
        ["wallet"],
    )
    op.create_index(
        "ix_wallet_closed_positions_market_id",
        "wallet_closed_positions",
        ["market_id"],
    )


def downgrade() -> None:
    op.drop_index("ix_wallet_closed_positions_market_id", table_name="wallet_closed_positions")
    op.drop_index("ix_wallet_closed_positions_wallet", table_name="wallet_closed_positions")
    op.drop_table("wallet_closed_positions")
