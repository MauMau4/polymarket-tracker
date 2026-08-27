import uuid
from datetime import datetime
from sqlalchemy import DateTime, Numeric, String, func
from sqlalchemy.orm import Mapped, mapped_column
from app.db.base import Base


class Position(Base):
    """System-inferred wallet positions built from accumulated trade history."""

    __tablename__ = "positions"

    id: Mapped[str] = mapped_column(String, primary_key=True, default=lambda: str(uuid.uuid4()))
    wallet: Mapped[str] = mapped_column(String, nullable=False, index=True)
    asset_id: Mapped[str] = mapped_column(String, nullable=False, index=True)
    market_id: Mapped[str] = mapped_column(String, nullable=False)
    balance: Mapped[float] = mapped_column(Numeric(precision=18, scale=6), nullable=False)
    est_cost_basis: Mapped[float | None] = mapped_column(Numeric(precision=18, scale=6), nullable=True)
    market_value: Mapped[float | None] = mapped_column(Numeric(precision=18, scale=6), nullable=True)
    snapshot_ts: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
