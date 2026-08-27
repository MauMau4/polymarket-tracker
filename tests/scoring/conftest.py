"""WalletClosedPosition / Wallet seed helpers for scoring tests.

Shared real-Postgres fixtures (event_loop_policy, db_session, migration-head
check) live in tests/conftest.py — reused by tests/booklog too.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone

from app.db.models.wallet import Wallet
from app.db.models.wallet_closed_position import WalletClosedPosition


async def add_wcp(
    session,
    *,
    wallet: str,
    closed_at: datetime,
    entry_price: float,
    exit_price: float,
    shares_sold: float = 100.0,
    is_resolved: bool = False,
    closed_at_source: str = "observed_exit",
    market_id: str | None = None,
    asset_id: str | None = None,
) -> WalletClosedPosition:
    """Insert a WalletClosedPosition with realistic derived fields (mirrors
    the invariants exit_skill.py enforces: cost_basis = entry*shares,
    realized_pnl = (exit-entry)*shares)."""
    cost_basis = entry_price * shares_sold
    realized_pnl = (exit_price - entry_price) * shares_sold
    row = WalletClosedPosition(
        id=str(uuid.uuid4()),
        wallet=wallet,
        asset_id=asset_id or str(uuid.uuid4()),
        market_id=market_id or str(uuid.uuid4()),
        entry_price=entry_price,
        exit_price=exit_price,
        shares_sold=shares_sold,
        total_shares_at_time=shares_sold,
        position_fraction=1.0,
        cost_basis=cost_basis,
        conviction_weight=0.1,
        log_roi=0.0,
        realized_pnl=realized_pnl,
        is_resolved=is_resolved,
        closed_at=closed_at,
        closed_at_source=closed_at_source,
    )
    session.add(row)
    return row


async def add_wallet(session, *, wallet: str, suspected_sybil: bool = False) -> Wallet:
    now = datetime.now(tz=timezone.utc)
    row = Wallet(
        wallet=wallet,
        first_seen=now,
        last_seen=now,
        suspected_sybil=suspected_sybil,
    )
    session.add(row)
    return row
