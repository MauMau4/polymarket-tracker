import uuid
from datetime import datetime
from sqlalchemy import Boolean, DateTime, Float, String, UniqueConstraint, Index
from sqlalchemy.orm import Mapped, mapped_column
from app.db.base import Base


class MarketPriceSnapshot(Base):
    """One-per-bucket price snapshot for favorite-longshot bias analysis.

    Written by run_price_snapshot.take_snapshot() at T-48 and T-24 windows.
    Updated by resolution.py when the market settles.
    Rows are never deleted.
    """

    __tablename__ = "market_price_snapshots"

    id: Mapped[str] = mapped_column(
        String, primary_key=True, default=lambda: str(uuid.uuid4())
    )
    market_id: Mapped[str] = mapped_column(String, nullable=False)
    snapshot_ts: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    hours_to_close: Mapped[float] = mapped_column(Float, nullable=False)
    snapshot_bucket: Mapped[str] = mapped_column(String, nullable=False)  # 'T24' or 'T48'
    yes_price: Mapped[float] = mapped_column(Float, nullable=False)
    no_price: Mapped[float] = mapped_column(Float, nullable=False)
    high_side: Mapped[str] = mapped_column(String, nullable=False)  # 'Yes' or 'No'
    high_side_price: Mapped[float] = mapped_column(Float, nullable=False)
    resolved: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    resolution: Mapped[str | None] = mapped_column(String, nullable=True)
    high_side_won: Mapped[bool | None] = mapped_column(Boolean, nullable=True)

    __table_args__ = (
        UniqueConstraint(
            "market_id", "snapshot_bucket",
            name="uq_market_price_snapshots_market_bucket",
        ),
        Index("ix_market_price_snapshots_market_id", "market_id"),
        Index("ix_market_price_snapshots_resolved", "resolved"),
    )
