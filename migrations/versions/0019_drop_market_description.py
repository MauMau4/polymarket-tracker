"""drop_market_description

Remove the description column from the markets table.
It is never displayed in the UI and consumes ~12x more storage than question.

Revision ID: 0019
Revises: 0018
Create Date: 2026-05-08 00:00:00.000000
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0019"
down_revision: Union[str, None] = "0018"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.drop_column("markets", "description")


def downgrade() -> None:
    op.add_column("markets", sa.Column("description", sa.Text, nullable=True))
