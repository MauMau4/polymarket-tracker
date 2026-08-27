import uuid
from datetime import datetime
from sqlalchemy import Boolean, DateTime, Float, String, Index, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column
from app.db.base import Base


class WalletPosition(Base):
    """Running per-(wallet, asset_id) position tracker.

    Maintained on every BUY, SELL, and market resolution event.
    Holds the current state of the position: shares, avg entry, realized PnL, status.
    """

    __tablename__ = "wallet_positions"

    id: Mapped[str] = mapped_column(
        String, primary_key=True, default=lambda: str(uuid.uuid4())
    )
    wallet: Mapped[str] = mapped_column(String, nullable=False)
    asset_id: Mapped[str] = mapped_column(String, nullable=False)
    market_id: Mapped[str] = mapped_column(String, nullable=False)
    selection: Mapped[str | None] = mapped_column(String, nullable=True)
    total_shares_bought: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    total_shares_sold: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    remaining_shares: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    avg_entry_price: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    total_cost_basis: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    realized_pnl: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    status: Mapped[str] = mapped_column(String, nullable=False, default="Open")
    estimated_entry: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    opened_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    closed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    __table_args__ = (
        UniqueConstraint("wallet", "asset_id", name="uq_wallet_positions_wallet_asset"),
        Index("ix_wallet_positions_wallet", "wallet"),
        Index("ix_wallet_positions_status", "status"),
        Index("ix_wallet_positions_market_status", "market_id", "status"),
    )
