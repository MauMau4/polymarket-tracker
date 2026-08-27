"""fix_farming_tags — remove incorrect farming tags

The prior scorer.py _is_farming() used OR logic (price > 0.90 OR days > 60),
which incorrectly tagged long-dated trades at any price (e.g. P=0.26, P=0.62)
as farming. The correct criterion requires BOTH conditions simultaneously AND
applies only to BUY orders.

This migration resets trade_type to NULL for all trades that are tagged as
farming but do not satisfy the correct AND criteria:
  side = 'BUY' AND price > 0.90

Trades where price > 0.90 AND side = BUY but days <= 60 are also wrong but
require a market join with date arithmetic; the scorer's tag_farming_trades()
function will handle those correctly on the next scheduled score refresh since
it now uses AND logic and BUY-only filtering.

Revision ID: 0008
Revises: 0007
Create Date: 2026-04-27 00:00:00.000000
"""
from typing import Sequence, Union
from alembic import op

revision: str = "0008"
down_revision: Union[str, None] = "0007"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Remove farming tag from any trade that is clearly not farming:
    # - side is not BUY (SELLs, nulls)
    # - price is at or below the ceiling (0.90) — the main case causing false positives
    op.execute("""
        UPDATE trades
        SET trade_type = NULL
        WHERE trade_type = 'farming'
          AND (
            side IS NULL
            OR side != 'BUY'
            OR price IS NULL
            OR price <= 0.90
          )
    """)


def downgrade() -> None:
    pass
