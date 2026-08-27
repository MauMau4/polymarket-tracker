import uuid
from datetime import datetime
from sqlalchemy import DateTime, ForeignKey, String, func
from sqlalchemy.orm import Mapped, mapped_column
from app.db.base import Base


class Token(Base):
    __tablename__ = "tokens"

    id: Mapped[str] = mapped_column(String, primary_key=True, default=lambda: str(uuid.uuid4()))
    asset_id: Mapped[str] = mapped_column(String, unique=True, nullable=False, index=True)
    market_id: Mapped[str] = mapped_column(
        String, ForeignKey("markets.market_id"), nullable=False, index=True
    )
    outcome: Mapped[str | None] = mapped_column(String, nullable=True)
    token_id: Mapped[str | None] = mapped_column(String, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    def __repr__(self) -> str:
        return f"<Token {self.asset_id} outcome={self.outcome}>"
