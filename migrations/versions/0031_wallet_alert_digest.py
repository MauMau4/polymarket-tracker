"""wallet_alert_digest

Alert-fatigue reduction (decisions/2026-07-19.md, item 1): lets specific
high-volume watched wallets be switched from real-time per-trade WATCH_WALLET
pings to a once-daily batched digest, independent of `watch_status` (which
still drives subscription augmentation elsewhere and must not change meaning).
Additive only: nullable-default boolean, defaults every existing wallet to
the current real-time behavior (False).

Revision ID: 0031
Revises: 0030
Create Date: 2026-07-19 00:00:00.000000
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0031"
down_revision: Union[str, None] = "0030"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "wallets",
        sa.Column("alert_digest", sa.Boolean, nullable=False, server_default="false"),
    )


def downgrade() -> None:
    op.drop_column("wallets", "alert_digest")
