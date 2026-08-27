"""wallet_positions

Add wallet_positions table: per-(wallet, asset_id) running position tracker
with shares, avg entry price, realized PnL, and Open/Closed status.

Revision ID: 0016
Revises: 0015
Create Date: 2026-05-07 00:00:00.000000
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0016"
down_revision: Union[str, None] = "0015"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "wallet_positions",
        sa.Column("id", sa.String, primary_key=True),
        sa.Column("wallet", sa.String, nullable=False),
        sa.Column("asset_id", sa.String, nullable=False),
        sa.Column("market_id", sa.String, nullable=False),
        sa.Column("selection", sa.String, nullable=True),
        sa.Column("total_shares_bought", sa.Float, nullable=False, server_default="0.0"),
        sa.Column("total_shares_sold", sa.Float, nullable=False, server_default="0.0"),
        sa.Column("remaining_shares", sa.Float, nullable=False, server_default="0.0"),
        sa.Column("avg_entry_price", sa.Float, nullable=False, server_default="0.0"),
        sa.Column("total_cost_basis", sa.Float, nullable=False, server_default="0.0"),
        sa.Column("realized_pnl", sa.Float, nullable=False, server_default="0.0"),
        sa.Column("status", sa.String, nullable=False, server_default="Open"),
        sa.Column("opened_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("closed_at", sa.DateTime(timezone=True), nullable=True),
        sa.UniqueConstraint("wallet", "asset_id", name="uq_wallet_positions_wallet_asset"),
    )
    op.create_index("ix_wallet_positions_wallet", "wallet_positions", ["wallet"])
    op.create_index("ix_wallet_positions_status", "wallet_positions", ["status"])


def downgrade() -> None:
    op.drop_index("ix_wallet_positions_status", table_name="wallet_positions")
    op.drop_index("ix_wallet_positions_wallet", table_name="wallet_positions")
    op.drop_table("wallet_positions")
