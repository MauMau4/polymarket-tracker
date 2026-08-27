import uuid
from datetime import datetime
from decimal import Decimal
from sqlalchemy import Boolean, DateTime, Integer, Numeric, String, func
from sqlalchemy.orm import Mapped, mapped_column
from app.db.base import Base


class PriceLevelAlert(Base):
    """Generic (market, side, level, direction) price-level crossing alert
    (decisions/2026-07-19.md item 3, C-NFL prerequisite). Watches one specific
    token (asset_id) and fires once per edge-triggered crossing of `level` in
    `direction`, with a cooldown as a flap-protection backstop — not a
    sustained-condition re-fire on every subsequent trade.

    `last_price` is the state used to detect the crossing edge: a row only
    fires when the previously observed price was on the opposite side of
    `level` from the current one. The first price ever observed for a new row
    just seeds `last_price` and never fires on its own (avoids a spurious
    immediate fire if the alert happens to be created after the level was
    already crossed).
    """

    __tablename__ = "price_level_alerts"

    id: Mapped[str] = mapped_column(String, primary_key=True, default=lambda: str(uuid.uuid4()))
    market_id: Mapped[str] = mapped_column(String, nullable=False, index=True)
    asset_id: Mapped[str] = mapped_column(String, nullable=False, index=True)
    label: Mapped[str | None] = mapped_column(String, nullable=True)
    level: Mapped[Decimal] = mapped_column(Numeric(precision=10, scale=6), nullable=False)
    # "above": fires when price crosses upward through level.
    # "below": fires when price crosses downward through level.
    direction: Mapped[str] = mapped_column(String, nullable=False)
    cooldown_seconds: Mapped[int] = mapped_column(Integer, nullable=False, default=300)
    active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)

    last_price: Mapped[Decimal | None] = mapped_column(Numeric(precision=10, scale=6), nullable=True)
    last_fired_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    fired_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    def __repr__(self) -> str:
        return f"<PriceLevelAlert {self.label or self.asset_id} {self.direction} {self.level}>"
