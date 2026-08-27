from datetime import datetime
from sqlalchemy import DateTime, Float, String, func
from sqlalchemy.orm import Mapped, mapped_column
from app.db.base import Base


class WalletCostBasis(Base):
    """Running cost basis per (wallet, asset_id) — updated at every BUY/SELL ingestion."""

    __tablename__ = "wallet_cost_basis"

    wallet: Mapped[str] = mapped_column(String, primary_key=True)
    asset_id: Mapped[str] = mapped_column(String, primary_key=True)
    market_id: Mapped[str] = mapped_column(String, nullable=False)
    avg_entry_price: Mapped[float] = mapped_column(Float, nullable=False)
    total_size: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
