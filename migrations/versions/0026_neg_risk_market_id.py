"""neg_risk_market_id

Pathfinder M1 Deliverable 4 prep. Additive only: nullable column, no data
change. Backfill runs as a separate, resumable task after this migration —
see app/tasks/run_backfill_neg_risk_market_id_20260715.py and
decisions/2026-07-15.md.

This column is safe for currently-running processes (run_worker.py,
run_ws_ingestor.py) on the OLD code: their in-memory ORM model doesn't know
about this column, so their upserts simply leave it NULL — no crash, no
schema mismatch. They must be restarted separately to start *populating* it
(see decisions/2026-07-15.md for exactly which services and why).

Revision ID: 0026
Revises: 0025
Create Date: 2026-07-15 00:00:00.000000
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0026"
down_revision: Union[str, None] = "0025"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "markets",
        sa.Column("neg_risk_market_id", sa.String(), nullable=True),
    )
    op.create_index(
        "ix_markets_neg_risk_market_id",
        "markets",
        ["neg_risk_market_id"],
    )


def downgrade() -> None:
    op.drop_index("ix_markets_neg_risk_market_id", table_name="markets")
    op.drop_column("markets", "neg_risk_market_id")
