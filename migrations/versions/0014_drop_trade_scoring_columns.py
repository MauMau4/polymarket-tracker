"""drop_trade_scoring_columns

Remove skill_score, edge_score, and exit_skill columns from the trades table.
These were used by the old Brier Edge scoring system, which has been replaced
by the ROI-based scoring system that reads from wallet_closed_positions.

IMPORTANT: Run the scoring backfill script (run_scoring_backfill.py) BEFORE
applying this migration, as the backfill reads exit_skill from this table.

Revision ID: 0014
Revises: 0013
Create Date: 2026-05-05 00:00:00.000000
"""
from typing import Sequence, Union
import sqlalchemy as sa
from alembic import op

revision: str = "0014"
down_revision: Union[str, None] = "0013"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.drop_column("trades", "skill_score")
    op.drop_column("trades", "edge_score")
    op.drop_column("trades", "exit_skill")


def downgrade() -> None:
    op.add_column("trades", sa.Column("skill_score", sa.Numeric(precision=10, scale=6), nullable=True))
    op.add_column("trades", sa.Column("edge_score", sa.Numeric(precision=10, scale=6), nullable=True))
    op.add_column("trades", sa.Column("exit_skill", sa.Numeric(precision=10, scale=6), nullable=True))
