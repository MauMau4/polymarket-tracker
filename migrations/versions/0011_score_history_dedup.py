"""score_history_dedup

One-time cleanup: keep only the most recent wallet_score_history entry per
wallet per calendar day (UTC), deleting earlier same-day duplicates.

Revision ID: 0011
Revises: 0010
Create Date: 2026-05-01 00:00:00.000000
"""
from typing import Sequence, Union
from alembic import op

revision: str = "0011"
down_revision: Union[str, None] = "0010"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute("""
        DELETE FROM wallet_score_history
        WHERE id IN (
            SELECT id FROM (
                SELECT id,
                       ROW_NUMBER() OVER (
                           PARTITION BY wallet, DATE(snapshot_ts AT TIME ZONE 'UTC')
                           ORDER BY snapshot_ts DESC
                       ) AS rn
                FROM wallet_score_history
            ) sub
            WHERE rn > 1
        )
    """)


def downgrade() -> None:
    # Deleted rows cannot be recovered; downgrade is a no-op.
    pass
