"""farming_cleanup_and_rebackfill — two fixes in one pass

1. Farming flag cleanup: The farming filter previously applied to ALL sides. Selling
   at a high price is profit-taking, not farming. Remove trade_type='farming' from any
   SELL trades (sets them back to NULL so they participate in scoring normally).

2. Outcome re-backfill: The discovery job now updates tokens.outcome when Gamma returns
   a corrected label (e.g. "Down" → "No"). Re-run the outcome sync from the tokens table
   so all existing trades pick up the corrected values.

Revision ID: 0007
Revises: 0006
Create Date: 2026-04-27 00:00:00.000000
"""
from typing import Sequence, Union
from alembic import op

revision: str = "0007"
down_revision: Union[str, None] = "0006"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # 1. Clear incorrect farming flags on SELL trades
    op.execute("""
        UPDATE trades
        SET trade_type = NULL
        WHERE side = 'SELL'
          AND trade_type = 'farming'
    """)

    # 2. Re-sync outcome labels from tokens table (picks up any corrections since migration 0006)
    op.execute("""
        UPDATE trades
        SET outcome = tok.outcome
        FROM tokens tok
        WHERE trades.asset_id = tok.asset_id
          AND (trades.outcome IS DISTINCT FROM tok.outcome)
    """)


def downgrade() -> None:
    # Neither change is reliably reversible without a pre-migration snapshot.
    pass
