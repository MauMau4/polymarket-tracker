import uuid
from datetime import datetime
from sqlalchemy import DateTime, String, func
from sqlalchemy.orm import Mapped, mapped_column
from app.db.base import Base


class EventCluster(Base):
    """Pathfinder market-to-event grouping (architecture §3.2, FR-2).

    NOT related to ClusterEvent/cluster_events (app/db/models/cluster_event.py),
    which detects coordinated-wallet trading (Sybil clusters) — an unrelated,
    pre-existing concept with a similar name. This table groups MARKETS that
    belong to the same real-world event, for portfolio exposure limiting
    (PRD §5.4 "max per event cluster").

    One row per market — a market belongs to at most one cluster. Re-seeding
    the heuristic (pathfinder/clusters/heuristics.py) must never overwrite a
    source='manual' row (see decisions/2026-07-15.md).
    """

    __tablename__ = "event_clusters"

    id: Mapped[str] = mapped_column(String, primary_key=True, default=lambda: str(uuid.uuid4()))
    cluster_id: Mapped[str] = mapped_column(String, nullable=False, index=True)
    market_id: Mapped[str] = mapped_column(String, nullable=False, unique=True)
    source: Mapped[str] = mapped_column(String, nullable=False)  # 'heuristic' | 'manual'
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )

    def __repr__(self) -> str:
        return f"<EventCluster market={self.market_id} cluster={self.cluster_id} source={self.source}>"
