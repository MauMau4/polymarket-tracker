"""pathfinder_scoring_pit

Pathfinder M1 Deliverable 2. Additive only:

- wallet_scores_pit: materialized point-in-time wallet scores
  (architecture §4). Brand-new table, no data change to existing tables.
- wallet_closed_positions (wallet, closed_at): composite index carried
  forward from the schema audit (docs/m1-schema-audit.md Q3) — the
  point-in-time formation-window query filters on both columns together.
  Built CONCURRENTLY per the session's live-system ground rules (this table
  is written by the live resolution job / exit_skill path continuously).

Revision ID: 0024
Revises: 0023
Create Date: 2026-07-15 00:00:00.000000
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision: str = "0024"
down_revision: Union[str, None] = "0023"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "wallet_scores_pit",
        sa.Column("wallet", sa.String(), nullable=False),
        sa.Column("as_of", sa.DateTime(timezone=True), nullable=False),
        sa.Column("config_version", sa.String(), nullable=False),
        sa.Column("qualified", sa.Boolean(), nullable=False),
        sa.Column("mean_roi", sa.Numeric(precision=14, scale=6), nullable=True),
        sa.Column("win_rate", sa.Numeric(precision=8, scale=6), nullable=True),
        sa.Column("implied_win_rate", sa.Numeric(precision=8, scale=6), nullable=True),
        sa.Column("n_positions", sa.Integer(), nullable=False),
        sa.Column("concentration_ratio", sa.Numeric(precision=8, scale=6), nullable=True),
        sa.Column("disqual_reasons", JSONB(), nullable=False),
        sa.Column("computed_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.PrimaryKeyConstraint("wallet", "as_of", "config_version"),
    )

    # CREATE INDEX CONCURRENTLY cannot run inside a transaction block.
    with op.get_context().autocommit_block():
        op.create_index(
            "ix_wcp_wallet_closed_at",
            "wallet_closed_positions",
            ["wallet", "closed_at"],
            unique=False,
            postgresql_concurrently=True,
        )


def downgrade() -> None:
    op.drop_index("ix_wcp_wallet_closed_at", table_name="wallet_closed_positions")
    op.drop_table("wallet_scores_pit")
