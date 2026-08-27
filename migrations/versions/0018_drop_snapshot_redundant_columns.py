"""drop_snapshot_redundant_columns

Remove question and condition_id from market_price_snapshots.
Both are redundant: question lives in markets.question,
condition_id is not needed since market_id is the join key.

Revision ID: 0018
Revises: 0017
Create Date: 2026-05-08 00:00:00.000000
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0018"
down_revision: Union[str, None] = "0017"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.drop_column("market_price_snapshots", "question")
    op.drop_column("market_price_snapshots", "condition_id")


def downgrade() -> None:
    op.add_column("market_price_snapshots", sa.Column("condition_id", sa.String, nullable=True))
    op.add_column("market_price_snapshots", sa.Column("question", sa.String, nullable=True))
