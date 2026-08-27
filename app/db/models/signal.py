import uuid
from datetime import datetime
from sqlalchemy import DateTime, Float, Index, String, UniqueConstraint, func
from sqlalchemy.orm import Mapped, mapped_column
from app.db.base import Base


class Signal(Base):
    """A detected accumulation event (architecture §3.3 / §4, PRD §5.2, FR-3).

    One row per (wallet, asset_id) net-notional crossing of
    config.signal.signal_min_notional_usd within a trailing
    accumulation_window_minutes window ("rising edge" only — see
    pathfinder/research/signals.py for the crossing-detection semantics).
    `side` is Polymarket's outcome label for asset_id (e.g. 'Yes'/'No'),
    not literally 'BUY'/'SELL' — a market's two outcome tokens are the two
    "directions" PRD §5.2 refers to; carried through from trades_full.outcome
    for report readability, nullable since a small fraction of trade rows
    have no attributed outcome label.

    Rows here are PRD-filtered candidates (price band, time-to-resolution,
    volume floor, and point-in-time wallet qualification all already
    applied) — not the raw crossing detections, which are cheap to
    regenerate from trades_full and are not persisted.
    """

    __tablename__ = "signals"

    id: Mapped[str] = mapped_column(String, primary_key=True, default=lambda: str(uuid.uuid4()))
    wallet: Mapped[str] = mapped_column(String, nullable=False)
    market_id: Mapped[str] = mapped_column(String, nullable=False)
    asset_id: Mapped[str] = mapped_column(String, nullable=False)
    side: Mapped[str | None] = mapped_column(String, nullable=True)
    signal_time: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    detected_time: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    signal_price: Mapped[float] = mapped_column(Float, nullable=False)
    accumulated_notional: Mapped[float] = mapped_column(Float, nullable=False)
    config_version: Mapped[str] = mapped_column(String, nullable=False)
    # 'backtest' | 'paper' | 'live' — only 'backtest' is reachable before Gate 3.
    phase: Mapped[str] = mapped_column(String, nullable=False, default="backtest")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    __table_args__ = (
        Index("ix_signals_wallet_signal_time", "wallet", "signal_time"),
        Index("ix_signals_market_id", "market_id"),
        UniqueConstraint(
            "wallet", "asset_id", "signal_time", "config_version",
            name="uq_signals_wallet_asset_signal_config",
        ),
    )

    def __repr__(self) -> str:
        return f"<Signal wallet={self.wallet} asset={self.asset_id} t={self.signal_time}>"
