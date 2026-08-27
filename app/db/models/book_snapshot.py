import uuid
from datetime import datetime
from decimal import Decimal
from sqlalchemy import DateTime, Index, Numeric, String, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column
from app.db.base import Base


class BookSnapshot(Base):
    """Top-of-book snapshot (architecture §3.5 / §4, FR-5).

    One row per (market, capture time). `asset_id` is the token actually
    snapshotted — the token labeled 'Yes' where the market has one, else the
    lowest asset_id token for that market (see pathfinder/booklog/snapshotter.py)
    — so best_bid/best_ask are always the same side for a given market across
    every row, making the time series comparable.
    """

    __tablename__ = "book_snapshots"

    id: Mapped[str] = mapped_column(String, primary_key=True, default=lambda: str(uuid.uuid4()))
    market_id: Mapped[str] = mapped_column(String, nullable=False)
    asset_id: Mapped[str] = mapped_column(String, nullable=False)
    ts: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    best_bid: Mapped[Decimal | None] = mapped_column(Numeric(precision=10, scale=6), nullable=True)
    best_ask: Mapped[Decimal | None] = mapped_column(Numeric(precision=10, scale=6), nullable=True)
    bid_sz: Mapped[Decimal | None] = mapped_column(Numeric(precision=18, scale=6), nullable=True)
    ask_sz: Mapped[Decimal | None] = mapped_column(Numeric(precision=18, scale=6), nullable=True)
    # {"bids": [{"price": "0.42", "size": "1000"}, ...top N], "asks": [...]}
    # best-to-worst order, N = config.booklog.book_depth_levels.
    levels: Mapped[dict] = mapped_column(JSONB, nullable=False)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    __table_args__ = (
        Index("ix_book_snapshots_market_ts", "market_id", "ts"),
    )

    def __repr__(self) -> str:
        return f"<BookSnapshot {self.market_id} ts={self.ts} bid={self.best_bid} ask={self.best_ask}>"
