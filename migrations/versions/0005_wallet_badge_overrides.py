"""wallet_badge_overrides — manual override booleans for INFORMED and FARMER badges

Revision ID: 0005
Revises: 0004
Create Date: 2026-04-26 00:00:00.000000
"""
from typing import Sequence, Union
import sqlalchemy as sa
from alembic import op

revision: str = "0005"
down_revision: Union[str, None] = "0004"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "wallets",
        sa.Column("manual_informed", sa.Boolean(), nullable=True),
    )
    op.add_column(
        "wallets",
        sa.Column("manual_farmer", sa.Boolean(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("wallets", "manual_farmer")
    op.drop_column("wallets", "manual_informed")
