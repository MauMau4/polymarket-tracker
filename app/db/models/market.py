import uuid
from datetime import datetime
from sqlalchemy import Boolean, DateTime, Float, Index, String, Text, func, text
from sqlalchemy.orm import Mapped, mapped_column
from app.db.base import Base


class Market(Base):
    __tablename__ = "markets"

    __table_args__ = (
        # Resolver candidate scan: unresolved markets past their end_date
        Index(
            "ix_markets_unresolved_end_date",
            "end_date",
            postgresql_where=text("resolved = false"),
        ),
    )

    id: Mapped[str] = mapped_column(String, primary_key=True, default=lambda: str(uuid.uuid4()))
    market_id: Mapped[str] = mapped_column(String, unique=True, nullable=False, index=True)
    condition_id: Mapped[str | None] = mapped_column(String, nullable=True, index=True)
    slug: Mapped[str | None] = mapped_column(String, nullable=True)
    question: Mapped[str | None] = mapped_column(Text, nullable=True)
    event_title: Mapped[str | None] = mapped_column(String, nullable=True)
    category: Mapped[str | None] = mapped_column(String, nullable=True)
    subcategory: Mapped[str | None] = mapped_column(String, nullable=True)
    active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    closed: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    resolved: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    resolution: Mapped[str | None] = mapped_column(String, nullable=True)
    # True settlement time (Gamma closedTime/umaEndDate), distinct from end_date
    # (scheduled close) — see docs/m1-schema-audit.md. NULL for legacy rows
    # pending backfill and for markets that were never genuinely resolved.
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    end_date: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # Polymarket's "neg risk" combinatorial-market group id — shared across
    # every mutually-exclusive outcome market in one categorical event (e.g.
    # all ~47 country markets in "who wins the World Cup"). Distinct from
    # event_title/groupItemTitle — see docs/m1-schema-audit.md addendum and
    # decisions/2026-07-15.md. NULL until backfilled / for markets Gamma
    # doesn't mark negRisk.
    neg_risk_market_id: Mapped[str | None] = mapped_column(String, nullable=True, index=True)
    volume: Mapped[float | None] = mapped_column(Float, nullable=True)
    # Last time the resolver checked this market against Gamma, regardless of
    # outcome. Drives the round-robin sweep of markets whose end_date hasn't
    # arrived yet but may have resolved early (tournament/election outright
    # winners) — see decisions/2026-07-17.md.
    resolution_checked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # Resolution value last used to stamp this market's trades.outcome_result /
    # wallet_closed_positions. A market is a re-stamp candidate whenever
    # `resolution IS DISTINCT FROM outcome_result_stamped_resolution` — true
    # once on first resolution, true again only if resolution later changes
    # (a correction). See migrations/versions/0034 and decisions/2026-08-06.md.
    outcome_result_stamped_resolution: Mapped[str | None] = mapped_column(String, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )

    def __repr__(self) -> str:
        return f"<Market {self.market_id} active={self.active}>"
