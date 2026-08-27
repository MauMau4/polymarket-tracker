"""signals

Pathfinder M2 (FR-3, architecture §3.3/§4). Additive only: brand new, empty
table — no existing data touched.

Revision ID: 0030
Revises: 0029
Create Date: 2026-07-17 00:00:00.000000
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0030"
down_revision: Union[str, None] = "0029"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "signals",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("wallet", sa.String(), nullable=False),
        sa.Column("market_id", sa.String(), nullable=False),
        sa.Column("asset_id", sa.String(), nullable=False),
        sa.Column("side", sa.String(), nullable=True),
        sa.Column("signal_time", sa.DateTime(timezone=True), nullable=False),
        sa.Column("detected_time", sa.DateTime(timezone=True), nullable=False),
        sa.Column("signal_price", sa.Float(), nullable=False),
        sa.Column("accumulated_notional", sa.Float(), nullable=False),
        sa.Column("config_version", sa.String(), nullable=False),
        sa.Column("phase", sa.String(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "wallet", "asset_id", "signal_time", "config_version",
            name="uq_signals_wallet_asset_signal_config",
        ),
    )
    op.create_index("ix_signals_wallet_signal_time", "signals", ["wallet", "signal_time"])
    op.create_index("ix_signals_market_id", "signals", ["market_id"])


def downgrade() -> None:
    op.drop_index("ix_signals_market_id", table_name="signals")
    op.drop_index("ix_signals_wallet_signal_time", table_name="signals")
    op.drop_table("signals")
