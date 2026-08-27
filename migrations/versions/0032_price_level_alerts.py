"""price_level_alerts

Generic (market, side, level, direction) price-level crossing alerts
(decisions/2026-07-19.md item 3, C-NFL prerequisite). Additive only: brand
new, empty table — no existing data touched.

Revision ID: 0032
Revises: 0031
Create Date: 2026-07-19 00:00:00.000000
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0032"
down_revision: Union[str, None] = "0031"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "price_level_alerts",
        sa.Column("id", sa.String, primary_key=True),
        sa.Column("market_id", sa.String, nullable=False),
        sa.Column("asset_id", sa.String, nullable=False),
        sa.Column("label", sa.String, nullable=True),
        sa.Column("level", sa.Numeric(precision=10, scale=6), nullable=False),
        sa.Column("direction", sa.String, nullable=False),
        sa.Column("cooldown_seconds", sa.Integer, nullable=False, server_default="300"),
        sa.Column("active", sa.Boolean, nullable=False, server_default="true"),
        sa.Column("last_price", sa.Numeric(precision=10, scale=6), nullable=True),
        sa.Column("last_fired_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("fired_count", sa.Integer, nullable=False, server_default="0"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )
    op.create_index("ix_price_level_alerts_market_id", "price_level_alerts", ["market_id"])
    op.create_index("ix_price_level_alerts_asset_id", "price_level_alerts", ["asset_id"])


def downgrade() -> None:
    op.drop_index("ix_price_level_alerts_asset_id", table_name="price_level_alerts")
    op.drop_index("ix_price_level_alerts_market_id", table_name="price_level_alerts")
    op.drop_table("price_level_alerts")
