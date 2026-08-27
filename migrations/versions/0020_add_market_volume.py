"""add_market_volume

Add a nullable volume column to the markets table.
Stores total USDC traded (lifetime). Populated on dashboard price fetches.
NULL means volume has never been fetched for this market.

Revision ID: 0020
Revises: 0019
Create Date: 2026-05-20 00:00:00.000000
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0020"
down_revision: Union[str, None] = "0019"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("markets", sa.Column("volume", sa.Float, nullable=True))


def downgrade() -> None:
    op.drop_column("markets", "volume")
