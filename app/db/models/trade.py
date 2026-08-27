import enum
import uuid
from datetime import datetime
import sqlalchemy as sa
from sqlalchemy import DateTime, Enum, Index, Numeric, String, Text, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column
from app.db.base import Base


class AttributionStatus(str, enum.Enum):
    attributed = "attributed"
    unresolved = "unresolved"
    unattributable = "unattributable"


class Trade(Base):
    __tablename__ = "trades"

    id: Mapped[str] = mapped_column(String, primary_key=True, default=lambda: str(uuid.uuid4()))
    external_trade_id: Mapped[str | None] = mapped_column(String, nullable=True, unique=True)
    ts: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    market_id: Mapped[str] = mapped_column(String, nullable=False)
    asset_id: Mapped[str] = mapped_column(String, nullable=False)
    outcome: Mapped[str | None] = mapped_column(String, nullable=True)
    price: Mapped[float | None] = mapped_column(Numeric(precision=18, scale=6), nullable=True)
    size: Mapped[float | None] = mapped_column(Numeric(precision=18, scale=6), nullable=True)
    notional_usd: Mapped[float | None] = mapped_column(Numeric(precision=18, scale=2), nullable=True)
    side: Mapped[str | None] = mapped_column(String, nullable=True)
    wallet: Mapped[str | None] = mapped_column(String, nullable=True)
    tx_hash: Mapped[str | None] = mapped_column(String, nullable=True)
    attribution_status: Mapped[AttributionStatus] = mapped_column(
        Enum(AttributionStatus, name="attribution_status_enum"),
        nullable=False,
        default=AttributionStatus.unresolved,
    )
    source: Mapped[str] = mapped_column(String, nullable=False)
    raw_payload: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    # NULL = normal, 'farming' = excluded from scoring, 'unwind' = position exit
    trade_type: Mapped[str | None] = mapped_column(String, nullable=True)
    # 1 = winning outcome, 0 = losing outcome, NULL = market not yet resolved
    outcome_result: Mapped[int | None] = mapped_column(sa.SmallInteger(), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    __table_args__ = (
        Index("ix_trades_ts_desc", "ts"),
        Index("ix_trades_wallet_ts", "wallet", "ts"),
        Index("ix_trades_market_id_ts", "market_id", "ts"),
        Index("ix_trades_asset_id_ts", "asset_id", "ts"),
        Index("ix_trades_wallet_side", "wallet", "side"),
    )

    def __repr__(self) -> str:
        return f"<Trade {self.id} asset={self.asset_id} status={self.attribution_status}>"
