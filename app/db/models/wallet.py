from datetime import datetime
from sqlalchemy import Boolean, DateTime, Float, Integer, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column
from app.db.base import Base


class Wallet(Base):
    __tablename__ = "wallets"

    wallet: Mapped[str] = mapped_column(String, primary_key=True)
    first_seen: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    last_seen: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    manual_tag: Mapped[str | None] = mapped_column(String, nullable=True)
    watch_status: Mapped[str | None] = mapped_column(String, nullable=True, index=True)
    # Alert-fatigue reduction (decisions/2026-07-19.md item 1): when True, this
    # wallet's WATCH_WALLET alerts are batched into one daily digest message
    # instead of real-time pings. Independent of watch_status, which still
    # drives subscription-augmentation semantics elsewhere and must not change.
    alert_digest: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    markets_traded_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    resolved_markets_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    estimated_win_rate: Mapped[float | None] = mapped_column(Float, nullable=True)
    estimated_realized_pnl: Mapped[float | None] = mapped_column(Float, nullable=True)
    early_entry_score: Mapped[float | None] = mapped_column(Float, nullable=True)
    concentration_score: Mapped[float | None] = mapped_column(Float, nullable=True)
    consistency_score: Mapped[float | None] = mapped_column(Float, nullable=True)
    wallet_score: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    score_confidence: Mapped[str] = mapped_column(String, nullable=False, default="low")
    suspected_sybil: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)

    # Phase 5+ aggregate stats (Welford-maintained, no historical scan needed)
    avg_skill_score: Mapped[float | None] = mapped_column(Float, nullable=True)
    avg_edge_score: Mapped[float | None] = mapped_column(Float, nullable=True)
    skill_consistency: Mapped[float | None] = mapped_column(Float, nullable=True)
    win_rate: Mapped[float | None] = mapped_column(Float, nullable=True)
    realized_pnl: Mapped[float | None] = mapped_column(Float, nullable=True)
    resolved_trades_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    score_computed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # Welford online algorithm state: running sum of squared deviations for skill
    welford_skill_m2: Mapped[float | None] = mapped_column(Float, nullable=True)
    # Badge helper: True if any trade for this wallet is tagged 'farming'
    has_farming_trades: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    # Average exit skill across all scored SELL trades (sell_price - avg_entry_price)
    avg_exit_skill: Mapped[float | None] = mapped_column(Float, nullable=True)
    # Manual badge overrides: True=force ON, False=force OFF, None=use system
    manual_informed: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    manual_farmer: Mapped[bool | None] = mapped_column(Boolean, nullable=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )

    def __repr__(self) -> str:
        return f"<Wallet {self.wallet} score={self.wallet_score}>"
