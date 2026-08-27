import uuid
from datetime import datetime
from sqlalchemy import DateTime, Float, Numeric, String, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column
from app.db.base import Base


class ClusterEvent(Base):
    __tablename__ = "cluster_events"

    id: Mapped[str] = mapped_column(String, primary_key=True, default=lambda: str(uuid.uuid4()))
    market_id: Mapped[str] = mapped_column(String, nullable=False, index=True)
    cluster_key: Mapped[str] = mapped_column(String, nullable=False)
    wallets: Mapped[list] = mapped_column(JSONB, nullable=False, default=list)
    direction: Mapped[str | None] = mapped_column(String, nullable=True)
    aggregate_notional: Mapped[float] = mapped_column(Numeric(precision=18, scale=2), nullable=False)
    start_ts: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    end_ts: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    confidence: Mapped[float | None] = mapped_column(Float, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
