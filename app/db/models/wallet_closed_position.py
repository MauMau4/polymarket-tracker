import uuid
from datetime import datetime
from sqlalchemy import Boolean, DateTime, Float, String, Index
from sqlalchemy.orm import Mapped, mapped_column
from app.db.base import Base


class WalletClosedPosition(Base):
    """Append-only record of every closed position event.

    Written at two points:
      - SELL trade ingestion (is_resolved=False)
      - Market resolution (is_resolved=True, covers remaining open position)

    Rows are never updated or deleted after being written.
    """

    __tablename__ = "wallet_closed_positions"

    id: Mapped[str] = mapped_column(
        String, primary_key=True, default=lambda: str(uuid.uuid4())
    )
    wallet: Mapped[str] = mapped_column(String, nullable=False)
    asset_id: Mapped[str] = mapped_column(String, nullable=False)
    market_id: Mapped[str] = mapped_column(String, nullable=False)
    entry_price: Mapped[float] = mapped_column(Float, nullable=False)
    exit_price: Mapped[float] = mapped_column(Float, nullable=False)
    shares_sold: Mapped[float] = mapped_column(Float, nullable=False)
    total_shares_at_time: Mapped[float] = mapped_column(Float, nullable=False)
    position_fraction: Mapped[float] = mapped_column(Float, nullable=False)
    cost_basis: Mapped[float] = mapped_column(Float, nullable=False)
    conviction_weight: Mapped[float] = mapped_column(Float, nullable=False)
    log_roi: Mapped[float] = mapped_column(Float, nullable=False)
    realized_pnl: Mapped[float] = mapped_column(Float, nullable=False)
    is_resolved: Mapped[bool] = mapped_column(Boolean, nullable=False)
    closed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    # Pre-correction value of closed_at, preserved for audit (2026-07-14 backfill).
    closed_at_original: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # Provenance of the current closed_at value: 'observed_exit' (organic SELL,
    # trade ts — always correct), 'gamma_settlement' (backfilled from Gamma
    # closedTime), 'operator_forced' (manual force-resolve script),
    # 'end_date_proxy' (unbackfilled legacy value — see docs/m1-schema-audit.md).
    closed_at_source: Mapped[str | None] = mapped_column(String, nullable=True)

    __table_args__ = (
        Index("ix_wallet_closed_positions_wallet", "wallet"),
        Index("ix_wallet_closed_positions_market_id", "market_id"),
        Index("ix_wcp_wallet_asset", "wallet", "asset_id"),
    )
