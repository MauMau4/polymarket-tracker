"""Trade / Market seed helpers for signal-enumeration tests.

Shared real-Postgres fixtures (event_loop_policy, db_session, session_factory,
migration-head check) live in tests/conftest.py.
"""
from __future__ import annotations

import uuid
from datetime import datetime

from app.db.models.market import Market
from app.db.models.token import Token
from app.db.models.trade import Trade


async def add_trade(
    session,
    *,
    wallet: str,
    asset_id: str,
    market_id: str,
    ts: datetime,
    price: float,
    size: float,
    side: str,
    outcome: str | None = "Yes",
) -> Trade:
    row = Trade(
        id=str(uuid.uuid4()),
        ts=ts,
        market_id=market_id,
        asset_id=asset_id,
        outcome=outcome,
        price=price,
        size=size,
        notional_usd=round(price * size, 2),
        side=side,
        wallet=wallet,
        attribution_status="attributed",
        source="manual",
    )
    session.add(row)
    return row


async def add_filler_volume(
    session,
    *,
    market_id: str,
    ts: datetime,
    total_notional: float,
    n: int = 25,
) -> None:
    """Spread `total_notional` of BUY volume across `n` distinct one-shot
    wallets (each trade under any plausible signal_min_notional_usd), so the
    filler volume counts toward a market's trailing volume without any
    filler wallet independently triggering its own accumulation crossing."""
    per_trade = total_notional / n
    for i in range(n):
        await add_trade(
            session, wallet=f"0xfiller{i}", asset_id="afiller", market_id=market_id,
            ts=ts, price=0.5, size=per_trade / 0.5, side="BUY",
        )


async def add_market(
    session,
    *,
    market_id: str,
    end_date: datetime | None,
    active: bool = True,
    resolved: bool = False,
    closed: bool | None = None,
    resolution: str | None = None,
) -> Market:
    # closed defaults to resolved (ck_markets_resolved_implies_closed,
    # migrations/0033) unless the caller explicitly overrides it.
    row = Market(
        id=str(uuid.uuid4()),
        market_id=market_id,
        active=active,
        resolved=resolved,
        closed=resolved if closed is None else closed,
        end_date=end_date,
        resolution=resolution,
    )
    session.add(row)
    return row


async def add_token(session, *, market_id: str, asset_id: str, outcome: str | None) -> Token:
    row = Token(id=str(uuid.uuid4()), asset_id=asset_id, market_id=market_id, outcome=outcome)
    session.add(row)
    return row
