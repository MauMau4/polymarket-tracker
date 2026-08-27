"""backfill_outcome_from_tokens — correct trade outcome labels using tokens table as source of truth

The Data API's outcome field is unreliable (can carry labels from unrelated markets).
This migration overwrites every trade's outcome with the value from the tokens table,
keyed by asset_id, which was populated from the Gamma API at discovery time.

Revision ID: 0006
Revises: 0005
Create Date: 2026-04-26 00:00:00.000000
"""
from typing import Sequence, Union
from alembic import op

revision: str = "0006"
down_revision: Union[str, None] = "0005"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute("""
        UPDATE trades
        SET outcome = tok.outcome
        FROM tokens tok
        WHERE trades.asset_id = tok.asset_id
          AND (trades.outcome IS DISTINCT FROM tok.outcome)
    """)


def downgrade() -> None:
    # Outcome values before migration are not recoverable — downgrade is a no-op.
    pass
