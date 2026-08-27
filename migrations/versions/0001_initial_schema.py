"""initial_schema

Revision ID: 0001
Revises:
Create Date: 2025-01-01 00:00:00.000000
"""
from typing import Sequence, Union
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB
from alembic import op

revision: str = "0001"
down_revision: Union[str, None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "markets",
        sa.Column("id", sa.String, primary_key=True),
        sa.Column("market_id", sa.String, nullable=False),
        sa.Column("condition_id", sa.String, nullable=True),
        sa.Column("slug", sa.String, nullable=True),
        sa.Column("question", sa.Text, nullable=True),
        sa.Column("description", sa.Text, nullable=True),
        sa.Column("event_title", sa.String, nullable=True),
        sa.Column("category", sa.String, nullable=True),
        sa.Column("active", sa.Boolean, nullable=False, server_default="true"),
        sa.Column("closed", sa.Boolean, nullable=False, server_default="false"),
        sa.Column("resolved", sa.Boolean, nullable=False, server_default="false"),
        sa.Column("resolution", sa.String, nullable=True),
        sa.Column("end_date", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    op.create_index("ix_markets_market_id", "markets", ["market_id"], unique=True)
    op.create_index("ix_markets_condition_id", "markets", ["condition_id"])
    op.create_index("ix_markets_active", "markets", ["active"])

    op.create_table(
        "tokens",
        sa.Column("id", sa.String, primary_key=True),
        sa.Column("asset_id", sa.String, nullable=False),
        sa.Column("market_id", sa.String, sa.ForeignKey("markets.market_id"), nullable=False),
        sa.Column("outcome", sa.String, nullable=True),
        sa.Column("token_id", sa.String, nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    op.create_index("ix_tokens_asset_id", "tokens", ["asset_id"], unique=True)
    op.create_index("ix_tokens_market_id", "tokens", ["market_id"])

    op.create_table(
        "trades",
        sa.Column("id", sa.String, primary_key=True),
        sa.Column("external_trade_id", sa.String, nullable=True),
        sa.Column("ts", sa.DateTime(timezone=True), nullable=False),
        sa.Column("market_id", sa.String, nullable=False),
        sa.Column("asset_id", sa.String, nullable=False),
        sa.Column("outcome", sa.String, nullable=True),
        sa.Column("price", sa.Numeric(18, 6), nullable=True),
        sa.Column("size", sa.Numeric(18, 6), nullable=True),
        sa.Column("notional_usd", sa.Numeric(18, 2), nullable=True),
        sa.Column("side", sa.String, nullable=True),
        sa.Column("wallet", sa.String, nullable=True),
        sa.Column("tx_hash", sa.String, nullable=True),
        sa.Column(
            "attribution_status",
            sa.Enum("attributed", "unresolved", "unattributable", name="attribution_status_enum"),
            nullable=False,
            server_default="unresolved",
        ),
        sa.Column("source", sa.String, nullable=False),
        sa.Column("raw_payload", JSONB, nullable=False, server_default="{}"),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    op.create_index("ix_trades_external_trade_id", "trades", ["external_trade_id"], unique=True)
    op.create_index("ix_trades_ts_desc", "trades", ["ts"])
    op.create_index("ix_trades_wallet_ts", "trades", ["wallet", "ts"])
    op.create_index("ix_trades_market_id_ts", "trades", ["market_id", "ts"])
    op.create_index("ix_trades_asset_id_ts", "trades", ["asset_id", "ts"])

    op.create_table(
        "wallets",
        sa.Column("wallet", sa.String, primary_key=True),
        sa.Column("first_seen", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_seen", sa.DateTime(timezone=True), nullable=False),
        sa.Column("manual_tag", sa.String, nullable=True),
        sa.Column("watch_status", sa.String, nullable=True),
        sa.Column("notes", sa.Text, nullable=True),
        sa.Column("markets_traded_count", sa.Integer, nullable=False, server_default="0"),
        sa.Column("resolved_markets_count", sa.Integer, nullable=False, server_default="0"),
        sa.Column("estimated_win_rate", sa.Float, nullable=True),
        sa.Column("estimated_realized_pnl", sa.Float, nullable=True),
        sa.Column("early_entry_score", sa.Float, nullable=True),
        sa.Column("concentration_score", sa.Float, nullable=True),
        sa.Column("consistency_score", sa.Float, nullable=True),
        sa.Column("wallet_score", sa.Float, nullable=False, server_default="0"),
        sa.Column("score_confidence", sa.String, nullable=False, server_default="low"),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    op.create_index("ix_wallets_watch_status", "wallets", ["watch_status"])
    op.create_index("ix_wallets_wallet_score", "wallets", ["wallet_score"])

    op.create_table(
        "positions",
        sa.Column("id", sa.String, primary_key=True),
        sa.Column("wallet", sa.String, nullable=False),
        sa.Column("asset_id", sa.String, nullable=False),
        sa.Column("market_id", sa.String, nullable=False),
        sa.Column("balance", sa.Numeric(18, 6), nullable=False),
        sa.Column("est_cost_basis", sa.Numeric(18, 6), nullable=True),
        sa.Column("market_value", sa.Numeric(18, 6), nullable=True),
        sa.Column("snapshot_ts", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_positions_wallet", "positions", ["wallet"])
    op.create_index("ix_positions_asset_id", "positions", ["asset_id"])

    op.create_table(
        "holder_snapshots",
        sa.Column("id", sa.String, primary_key=True),
        sa.Column("market_id", sa.String, nullable=False),
        sa.Column("wallet", sa.String, nullable=False),
        sa.Column("rank", sa.Integer, nullable=False),
        sa.Column("balance", sa.Numeric(18, 6), nullable=False),
        sa.Column("share_pct", sa.Float, nullable=True),
        sa.Column("snapshot_ts", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_holder_snapshots_market_id", "holder_snapshots", ["market_id"])
    op.create_index("ix_holder_snapshots_wallet", "holder_snapshots", ["wallet"])
    op.create_index("ix_holder_snapshots_snapshot_ts", "holder_snapshots", ["snapshot_ts"])

    op.create_table(
        "wallet_score_history",
        sa.Column("id", sa.String, primary_key=True),
        sa.Column("wallet", sa.String, nullable=False),
        sa.Column("score", sa.Float, nullable=False),
        sa.Column("confidence", sa.String, nullable=False),
        sa.Column("components", JSONB, nullable=False, server_default="{}"),
        sa.Column("snapshot_ts", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_wallet_score_history_wallet", "wallet_score_history", ["wallet"])

    op.create_table(
        "alerts",
        sa.Column("id", sa.String, primary_key=True),
        sa.Column("alert_type", sa.String, nullable=False),
        sa.Column("severity", sa.String, nullable=False),
        sa.Column("wallet", sa.String, nullable=True),
        sa.Column("market_id", sa.String, nullable=True),
        sa.Column("asset_id", sa.String, nullable=True),
        sa.Column("title", sa.String, nullable=False),
        sa.Column("message", sa.Text, nullable=False),
        sa.Column("payload", JSONB, nullable=False, server_default="{}"),
        sa.Column("dedupe_key", sa.String, nullable=False),
        sa.Column("sent", sa.Boolean, nullable=False, server_default="false"),
        sa.Column("sent_ts", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    op.create_index("ix_alerts_alert_type", "alerts", ["alert_type"])
    op.create_index("ix_alerts_severity", "alerts", ["severity"])
    op.create_index("ix_alerts_wallet", "alerts", ["wallet"])
    op.create_index("ix_alerts_dedupe_key", "alerts", ["dedupe_key"])

    op.create_table(
        "cluster_events",
        sa.Column("id", sa.String, primary_key=True),
        sa.Column("market_id", sa.String, nullable=False),
        sa.Column("cluster_key", sa.String, nullable=False),
        sa.Column("wallets", JSONB, nullable=False, server_default="[]"),
        sa.Column("direction", sa.String, nullable=True),
        sa.Column("aggregate_notional", sa.Numeric(18, 2), nullable=False),
        sa.Column("start_ts", sa.DateTime(timezone=True), nullable=False),
        sa.Column("end_ts", sa.DateTime(timezone=True), nullable=True),
        sa.Column("confidence", sa.Float, nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    op.create_index("ix_cluster_events_market_id", "cluster_events", ["market_id"])

    op.create_table(
        "settings",
        sa.Column("key", sa.String, primary_key=True),
        sa.Column("value", JSONB, nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )


def downgrade() -> None:
    op.drop_table("settings")
    op.drop_table("cluster_events")
    op.drop_table("alerts")
    op.drop_table("wallet_score_history")
    op.drop_table("holder_snapshots")
    op.drop_table("positions")
    op.drop_table("wallets")
    op.drop_index("ix_trades_asset_id_ts", table_name="trades")
    op.drop_index("ix_trades_market_id_ts", table_name="trades")
    op.drop_index("ix_trades_wallet_ts", table_name="trades")
    op.drop_index("ix_trades_ts_desc", table_name="trades")
    op.drop_index("ix_trades_external_trade_id", table_name="trades")
    op.drop_table("trades")
    op.execute("DROP TYPE attribution_status_enum")
    op.drop_table("tokens")
    op.drop_table("markets")
