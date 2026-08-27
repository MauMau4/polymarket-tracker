"""book_snapshots

Pathfinder M1 Deliverable 3 (FR-5, architecture §3.5). Additive only: brand
new, empty table — no existing data touched.

Revision ID: 0025
Revises: 0024
Create Date: 2026-07-15 00:00:00.000000
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision: str = "0025"
down_revision: Union[str, None] = "0024"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "book_snapshots",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("market_id", sa.String(), nullable=False),
        sa.Column("asset_id", sa.String(), nullable=False),
        sa.Column("ts", sa.DateTime(timezone=True), nullable=False),
        sa.Column("best_bid", sa.Numeric(precision=10, scale=6), nullable=True),
        sa.Column("best_ask", sa.Numeric(precision=10, scale=6), nullable=True),
        sa.Column("bid_sz", sa.Numeric(precision=18, scale=6), nullable=True),
        sa.Column("ask_sz", sa.Numeric(precision=18, scale=6), nullable=True),
        sa.Column("levels", JSONB(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_book_snapshots_market_ts",
        "book_snapshots",
        ["market_id", "ts"],
    )


def downgrade() -> None:
    op.drop_index("ix_book_snapshots_market_ts", table_name="book_snapshots")
    op.drop_table("book_snapshots")
