"""drop_holder_snapshots

Remove the holder_snapshots table and its indexes — top-holder feature removed.

Revision ID: 0012
Revises: 0011
Create Date: 2026-05-02 00:00:00.000000
"""
from typing import Sequence, Union
import sqlalchemy as sa
from alembic import op

revision: str = "0012"
down_revision: Union[str, None] = "0011"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.drop_index("ix_holder_snapshots_snapshot_ts", table_name="holder_snapshots")
    op.drop_index("ix_holder_snapshots_wallet", table_name="holder_snapshots")
    op.drop_index("ix_holder_snapshots_market_id", table_name="holder_snapshots")
    op.drop_table("holder_snapshots")


def downgrade() -> None:
    op.create_table(
        "holder_snapshots",
        sa.Column("id", sa.String, primary_key=True),
        sa.Column("market_id", sa.String, nullable=False),
        sa.Column("wallet", sa.String, nullable=False),
        sa.Column("rank", sa.Integer, nullable=False),
        sa.Column("balance", sa.Numeric(18, 6), nullable=False),
        sa.Column("share_pct", sa.Float, nullable=True),
        sa.Column("snapshot_ts", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_holder_snapshots_market_id", "holder_snapshots", ["market_id"])
    op.create_index("ix_holder_snapshots_wallet", "holder_snapshots", ["wallet"])
    op.create_index("ix_holder_snapshots_snapshot_ts", "holder_snapshots", ["snapshot_ts"])
