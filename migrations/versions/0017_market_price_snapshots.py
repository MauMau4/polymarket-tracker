"""market_price_snapshots

Add market_price_snapshots table for favorite-longshot bias analysis.
Stores one T-48 and one T-24 price snapshot per market (high-side >= 0.80).
Updated with resolution outcome once the market settles.

Revision ID: 0017
Revises: 0016
Create Date: 2026-05-08 00:00:00.000000
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0017"
down_revision: Union[str, None] = "0016"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "market_price_snapshots",
        sa.Column("id", sa.String, primary_key=True),
        sa.Column("market_id", sa.String, nullable=False),
        sa.Column("condition_id", sa.String, nullable=True),
        sa.Column("question", sa.String, nullable=True),
        sa.Column("snapshot_ts", sa.DateTime(timezone=True), nullable=False),
        sa.Column("hours_to_close", sa.Float, nullable=False),
        sa.Column("snapshot_bucket", sa.String, nullable=False),
        sa.Column("yes_price", sa.Float, nullable=False),
        sa.Column("no_price", sa.Float, nullable=False),
        sa.Column("high_side", sa.String, nullable=False),
        sa.Column("high_side_price", sa.Float, nullable=False),
        sa.Column("resolved", sa.Boolean, nullable=False, server_default="false"),
        sa.Column("resolution", sa.String, nullable=True),
        sa.Column("high_side_won", sa.Boolean, nullable=True),
        sa.UniqueConstraint(
            "market_id", "snapshot_bucket",
            name="uq_market_price_snapshots_market_bucket",
        ),
    )
    op.create_index(
        "ix_market_price_snapshots_market_id",
        "market_price_snapshots",
        ["market_id"],
    )
    op.create_index(
        "ix_market_price_snapshots_resolved",
        "market_price_snapshots",
        ["resolved"],
    )


def downgrade() -> None:
    op.drop_index("ix_market_price_snapshots_resolved", table_name="market_price_snapshots")
    op.drop_index("ix_market_price_snapshots_market_id", table_name="market_price_snapshots")
    op.drop_table("market_price_snapshots")
