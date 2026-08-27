"""Market / Token seed helpers — used by tests/booklog and tests/clusters."""
from __future__ import annotations

import uuid
from datetime import datetime

from app.db.models.market import Market
from app.db.models.token import Token


async def add_market(
    session,
    *,
    market_id: str,
    volume: float | None,
    active: bool = True,
    resolved: bool = False,
    closed: bool | None = None,
    event_title: str | None = None,
    end_date: datetime | None = None,
    neg_risk_market_id: str | None = None,
    question: str | None = None,
) -> Market:
    # closed defaults to resolved (ck_markets_resolved_implies_closed,
    # migrations/0033) unless the caller explicitly overrides it.
    row = Market(
        id=str(uuid.uuid4()),
        market_id=market_id,
        active=active,
        resolved=resolved,
        closed=resolved if closed is None else closed,
        volume=volume,
        event_title=event_title,
        end_date=end_date,
        neg_risk_market_id=neg_risk_market_id,
        question=question,
    )
    session.add(row)
    return row


async def add_token(session, *, market_id: str, asset_id: str, outcome: str | None) -> Token:
    row = Token(
        id=str(uuid.uuid4()),
        asset_id=asset_id,
        market_id=market_id,
        outcome=outcome,
    )
    session.add(row)
    return row
