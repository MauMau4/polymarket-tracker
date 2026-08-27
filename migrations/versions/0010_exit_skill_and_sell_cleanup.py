"""exit_skill_and_sell_cleanup

Adds exit skill tracking columns and cleans up incorrectly tagged SELL trades.

Changes:
  - trades: add exit_skill FLOAT column
  - wallets: add avg_exit_skill FLOAT column
  - create wallet_cost_basis table for running avg entry price per (wallet, asset_id)
  - clear trade_type='farming' from all SELL trades (SELLs are never farming)
  - recompute has_farming_trades on wallets whose SELL farming trades were cleaned

Revision ID: 0010
Revises: 0009
Create Date: 2026-04-27 00:00:00.000000
"""
from typing import Sequence, Union
import sqlalchemy as sa
from alembic import op

revision: str = "0010"
down_revision: Union[str, None] = "0009"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Add exit_skill to trades
    op.add_column("trades", sa.Column("exit_skill", sa.Numeric(precision=10, scale=6), nullable=True))

    # Add avg_exit_skill to wallets
    op.add_column("wallets", sa.Column("avg_exit_skill", sa.Float(), nullable=True))

    # Create wallet_cost_basis table
    op.create_table(
        "wallet_cost_basis",
        sa.Column("wallet", sa.String(), nullable=False),
        sa.Column("asset_id", sa.String(), nullable=False),
        sa.Column("market_id", sa.String(), nullable=False),
        sa.Column("avg_entry_price", sa.Float(), nullable=False),
        sa.Column("total_size", sa.Float(), nullable=False, server_default="0"),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.PrimaryKeyConstraint("wallet", "asset_id"),
    )
    op.create_index("ix_wallet_cost_basis_wallet", "wallet_cost_basis", ["wallet"])
    op.create_index("ix_wallet_cost_basis_asset_id", "wallet_cost_basis", ["asset_id"])

    # Clean up SELL trades incorrectly tagged as farming
    op.execute("""
        UPDATE trades
        SET trade_type = NULL
        WHERE trade_type = 'farming'
          AND side = 'SELL'
    """)

    # Recompute has_farming_trades: set to false for wallets with no remaining farming BUY trades
    op.execute("""
        UPDATE wallets
        SET has_farming_trades = false
        WHERE has_farming_trades = true
          AND wallet NOT IN (
              SELECT DISTINCT wallet
              FROM trades
              WHERE trade_type = 'farming'
                AND wallet IS NOT NULL
          )
    """)


def downgrade() -> None:
    op.drop_index("ix_wallet_cost_basis_asset_id", table_name="wallet_cost_basis")
    op.drop_index("ix_wallet_cost_basis_wallet", table_name="wallet_cost_basis")
    op.drop_table("wallet_cost_basis")
    op.drop_column("wallets", "avg_exit_skill")
    op.drop_column("trades", "exit_skill")
