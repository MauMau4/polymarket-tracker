import uuid
from datetime import datetime
from sqlalchemy import DateTime, Float, String, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column
from app.db.base import Base


class WalletScoreHistory(Base):
    __tablename__ = "wallet_score_history"

    id: Mapped[str] = mapped_column(String, primary_key=True, default=lambda: str(uuid.uuid4()))
    wallet: Mapped[str] = mapped_column(String, nullable=False, index=True)
    score: Mapped[float] = mapped_column(Float, nullable=False)
    confidence: Mapped[str] = mapped_column(String, nullable=False)
    components: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    snapshot_ts: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
