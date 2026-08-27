"""cost_basis_and_realized_pnl

Rename wallet_closed_positions.notional_usd → cost_basis and add realized_pnl.

Changes:
  1. Rename notional_usd → cost_basis on wallet_closed_positions.
  2. Add realized_pnl column = (exit_price - entry_price) * shares_sold.
  3. Fix cost_basis for SELL-sourced rows (is_resolved=False): was sell_price*shares,
     now standardised to entry_price*shares_sold across all row sources.
  4. Backfill realized_pnl for all existing rows.
  5. Recalculate conviction_weight using BUY-only lifetime notional denominator
     (previously included SELL trades, which double-counted capital).

Revision ID: 0015
Revises: 0014
Create Date: 2026-05-07 00:00:00.000000
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0015"
down_revision: Union[str, None] = "0014"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # 1. Rename notional_usd → cost_basis
    op.alter_column("wallet_closed_positions", "notional_usd", new_column_name="cost_basis")

    # 2. Add realized_pnl (nullable initially so we can backfill before constraining)
    op.add_column(
        "wallet_closed_positions",
        sa.Column("realized_pnl", sa.Float, nullable=True),
    )

    # 3. Fix cost_basis for SELL-sourced rows (is_resolved=FALSE).
    #    These were written as sell_price * shares_sold; correct value is entry_price * shares_sold.
    #    Resolved rows were already written correctly (avg_entry_price * total_size).
    op.execute("""
        UPDATE wallet_closed_positions
        SET cost_basis = entry_price * shares_sold
        WHERE is_resolved = FALSE
    """)

    # 4. Backfill realized_pnl for all existing rows.
    op.execute("""
        UPDATE wallet_closed_positions
        SET realized_pnl = (exit_price - entry_price) * shares_sold
    """)

    # 5. Make realized_pnl NOT NULL now that all rows have values.
    op.alter_column("wallet_closed_positions", "realized_pnl", nullable=False)

    # 6. Recalculate conviction_weight using BUY-only lifetime notional.
    #    Previous denominator included SELL trades, double-counting closed capital.
    #    Rows with no matching BUY trades in the trades table are left unchanged.
    op.execute("""
        WITH wallet_buy_notional AS (
            SELECT wallet, SUM(notional_usd) AS total_buy_notional
            FROM trades
            WHERE notional_usd IS NOT NULL
              AND side = 'BUY'
              AND wallet IS NOT NULL
            GROUP BY wallet
        )
        UPDATE wallet_closed_positions wcp
        SET conviction_weight = CASE
            WHEN wbn.total_buy_notional > 0
                THEN (wcp.entry_price * wcp.shares_sold) / wbn.total_buy_notional
            ELSE 0.0
        END
        FROM wallet_buy_notional wbn
        WHERE wbn.wallet = wcp.wallet
    """)


def downgrade() -> None:
    op.drop_column("wallet_closed_positions", "realized_pnl")
    op.alter_column("wallet_closed_positions", "cost_basis", new_column_name="notional_usd")
