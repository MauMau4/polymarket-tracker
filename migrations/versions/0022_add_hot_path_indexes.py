"""add_hot_path_indexes

Indexes for the hottest recurring queries:

- wallet_closed_positions (wallet, asset_id): duplicate-WCP checks on every
  resolution closure and buy-into-resolved-market ingestion.
- wallet_positions (market_id, status): resolver per-market open-position scans.
- trades (wallet, side): lifetime BUY-notional sums on every SELL/closure.
- markets partial (end_date) WHERE NOT resolved: resolver candidate scan.

Revision ID: 0022
Revises: 0021
Create Date: 2026-07-11 00:00:00.000000
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0022"
down_revision: Union[str, None] = "0021"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_index(
        "ix_wcp_wallet_asset",
        "wallet_closed_positions",
        ["wallet", "asset_id"],
    )
    op.create_index(
        "ix_wallet_positions_market_status",
        "wallet_positions",
        ["market_id", "status"],
    )
    op.create_index(
        "ix_trades_wallet_side",
        "trades",
        ["wallet", "side"],
    )
    op.create_index(
        "ix_markets_unresolved_end_date",
        "markets",
        ["end_date"],
        postgresql_where=sa.text("resolved = false"),
    )


def downgrade() -> None:
    op.drop_index("ix_markets_unresolved_end_date", table_name="markets")
    op.drop_index("ix_trades_wallet_side", table_name="trades")
    op.drop_index("ix_wallet_positions_market_status", table_name="wallet_positions")
    op.drop_index("ix_wcp_wallet_asset", table_name="wallet_closed_positions")
