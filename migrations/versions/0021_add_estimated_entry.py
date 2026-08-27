"""add_estimated_entry

Add estimated_entry boolean to wallet_positions.
True when the position was created as a placeholder from sell-price-as-entry
(no buy history available).

Also backfills existing placeholder rows: Closed positions where
total_shares_bought = total_shares_sold, realized_pnl = 0, total_cost_basis > 0,
avg_entry_price > 0, and opened_at = closed_at.

Revision ID: 0021
Revises: 0020
Create Date: 2026-05-25 00:00:00.000000
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0021"
down_revision: Union[str, None] = "0020"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "wallet_positions",
        sa.Column("estimated_entry", sa.Boolean, nullable=False, server_default="false"),
    )
    # Backfill existing placeholder positions created by the backfill script
    op.execute("""
        UPDATE wallet_positions
        SET estimated_entry = true
        WHERE status = 'Closed'
          AND total_shares_bought > 0
          AND ABS(total_shares_bought - total_shares_sold) < 0.001
          AND realized_pnl = 0.0
          AND total_cost_basis > 0
          AND avg_entry_price > 0
          AND (opened_at = closed_at OR opened_at IS NULL)
    """)


def downgrade() -> None:
    op.drop_column("wallet_positions", "estimated_entry")
