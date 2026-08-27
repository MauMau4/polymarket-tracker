"""clear_stale_farming_badges — sync has_farming_trades flag with trades table

Migration 0008 removed incorrect farming tags from trades but did not update
the has_farming_trades flag on wallet records. Any wallet whose farming trades
were all cleared now shows the FARMER badge incorrectly.

This migration resets has_farming_trades=false on every wallet that has no
remaining trades with trade_type='farming'.

Revision ID: 0009
Revises: 0008
Create Date: 2026-04-27 00:00:00.000000
"""
from typing import Sequence, Union
from alembic import op

revision: str = "0009"
down_revision: Union[str, None] = "0008"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute("""
        UPDATE wallets
        SET has_farming_trades = false
        WHERE has_farming_trades = true
          AND wallet NOT IN (
              SELECT DISTINCT wallet
              FROM trades
              WHERE trade_type = 'farming'
                AND wallet IS NOT NULL
          )
    """)


def downgrade() -> None:
    pass
