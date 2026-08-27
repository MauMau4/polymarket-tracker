"""event_clusters

Pathfinder M1 Deliverable 4 (FR-2, architecture §3.2). Additive only: brand
new, empty table — no existing data touched.

Revision ID: 0027
Revises: 0026
Create Date: 2026-07-15 00:00:00.000000
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0027"
down_revision: Union[str, None] = "0026"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "event_clusters",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("cluster_id", sa.String(), nullable=False),
        sa.Column("market_id", sa.String(), nullable=False),
        sa.Column("source", sa.String(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("market_id"),
    )
    op.create_index("ix_event_clusters_cluster_id", "event_clusters", ["cluster_id"])


def downgrade() -> None:
    op.drop_index("ix_event_clusters_cluster_id", table_name="event_clusters")
    op.drop_table("event_clusters")
