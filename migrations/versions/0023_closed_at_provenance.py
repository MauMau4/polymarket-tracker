"""closed_at_provenance

Additive only — no data changes. Adds the missing "true settlement time"
primitive (markets.resolved_at) and provenance tracking for
wallet_closed_positions.closed_at, ahead of the Pathfinder M1 backfill that
corrects closed_at from markets.end_date to the real Gamma settlement time.

See docs/m1-schema-audit.md and decisions/2026-07-14.md for the investigation
this responds to.

Revision ID: 0023
Revises: 0022
Create Date: 2026-07-14 00:00:00.000000
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0023"
down_revision: Union[str, None] = "0022"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "markets",
        sa.Column("resolved_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "wallet_closed_positions",
        sa.Column("closed_at_original", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "wallet_closed_positions",
        sa.Column("closed_at_source", sa.String(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("wallet_closed_positions", "closed_at_source")
    op.drop_column("wallet_closed_positions", "closed_at_original")
    op.drop_column("markets", "resolved_at")
