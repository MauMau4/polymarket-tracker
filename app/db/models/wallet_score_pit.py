from datetime import datetime
from decimal import Decimal
from sqlalchemy import Boolean, DateTime, Integer, Numeric, String, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column
from app.db.base import Base


class WalletScorePit(Base):
    """Materialized point-in-time Pathfinder wallet score (architecture §4).

    One row per (wallet, as_of, config_version) — never updated in place.
    Re-scoring the same (wallet, as_of) under a new config_version writes a
    new row rather than overwriting the old one, so past research runs stay
    reproducible against the config_version they were computed with.
    """

    __tablename__ = "wallet_scores_pit"

    wallet: Mapped[str] = mapped_column(String, primary_key=True)
    as_of: Mapped[datetime] = mapped_column(DateTime(timezone=True), primary_key=True)
    config_version: Mapped[str] = mapped_column(String, primary_key=True)

    qualified: Mapped[bool] = mapped_column(Boolean, nullable=False)
    mean_roi: Mapped[Decimal | None] = mapped_column(Numeric(precision=14, scale=6), nullable=True)
    win_rate: Mapped[Decimal | None] = mapped_column(Numeric(precision=8, scale=6), nullable=True)
    implied_win_rate: Mapped[Decimal | None] = mapped_column(Numeric(precision=8, scale=6), nullable=True)
    n_positions: Mapped[int] = mapped_column(Integer, nullable=False)
    concentration_ratio: Mapped[Decimal | None] = mapped_column(Numeric(precision=8, scale=6), nullable=True)
    disqual_reasons: Mapped[list] = mapped_column(JSONB, nullable=False, default=list)

    computed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    def __repr__(self) -> str:
        return f"<WalletScorePit {self.wallet} as_of={self.as_of} qualified={self.qualified}>"
