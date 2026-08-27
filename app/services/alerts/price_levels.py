"""Generic (market, side, level, direction) price-level crossing alerts
(decisions/2026-07-19.md item 3, C-NFL prerequisite). Independent of the
trade-alert engine (app/services/alerts/engine.py) — this fires on *price*
movement regardless of trade size, side, or wallet attribution, since a
level crossing matters the same whether it happened on a $5 or $50,000 print.

Fires once per edge-triggered crossing (previous observed price on the other
side of `level`, current price on the trigger side), with `cooldown_seconds`
as a flap-protection backstop against rapid oscillation right at the level.
INFO severity but delivered immediately — not batched into any digest.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Alert, PriceLevelAlert
from app.logging import get_logger
from app.services.alerts import telegram

logger = get_logger(__name__)


def _direction_satisfied(direction: str, price: Decimal, level: Decimal) -> bool:
    if direction == "above":
        return price >= level
    if direction == "below":
        return price <= level
    raise ValueError(f"unknown price_level_alert direction: {direction!r}")


def check_crossing(alert: PriceLevelAlert, price: Decimal, now: datetime) -> bool:
    """Pure predicate: True if this new price observation is a fresh,
    cooldown-clear crossing that should fire. Does not mutate `alert` —
    callers are responsible for updating last_price/last_fired_at/fired_count.
    """
    if not alert.active:
        return False

    satisfied_now = _direction_satisfied(alert.direction, price, alert.level)
    if not satisfied_now:
        return False

    prev = alert.last_price
    if prev is None:
        # First observation for this row only seeds state — never fires on
        # its own, so creating an alert after the level was already crossed
        # doesn't produce a spurious immediate "crossing".
        return False

    satisfied_before = _direction_satisfied(alert.direction, prev, alert.level)
    if satisfied_before:
        return False  # already on this side — not a fresh crossing

    if alert.last_fired_at is not None:
        elapsed = (now - alert.last_fired_at).total_seconds()
        if elapsed < alert.cooldown_seconds:
            return False

    return True


def format_price_level_message(alert: PriceLevelAlert, price: Decimal) -> str:
    arrow = "↑" if alert.direction == "above" else "↓"
    label = alert.label or f"{alert.market_id} / {alert.asset_id[:12]}..."
    return (
        f"[PRICE LEVEL] {arrow}\n\n"
        f"{label}\n"
        f"Crossed {alert.direction} {float(alert.level):.3f} — now {float(price):.3f}"
    )


async def evaluate_price_levels(session: AsyncSession, asset_id: str, price: Decimal, ts: datetime) -> int:
    """Check every active PriceLevelAlert watching `asset_id` for a fresh
    crossing at `price`; fire (Telegram + Alert row) for each, then update
    state. Commits the session. Returns the number of alerts fired."""
    rows = (
        (
            await session.execute(
                select(PriceLevelAlert).where(
                    PriceLevelAlert.asset_id == asset_id,
                    PriceLevelAlert.active == True,  # noqa: E712
                )
            )
        )
        .scalars()
        .all()
    )
    if not rows:
        return 0

    fired = 0
    for row in rows:
        if check_crossing(row, price, ts):
            text = format_price_level_message(row, price)
            sent = await telegram.send_message(text)
            session.add(
                Alert(
                    id=str(uuid.uuid4()),
                    alert_type="PRICE_LEVEL",
                    severity="info",
                    wallet=None,
                    market_id=row.market_id,
                    asset_id=row.asset_id,
                    title=f"PRICE_LEVEL — {row.label or row.market_id}",
                    message=text,
                    payload={
                        "level": float(row.level),
                        "direction": row.direction,
                        "price": float(price),
                        "price_level_alert_id": row.id,
                    },
                    dedupe_key=f"price_level:{row.id}:{ts.timestamp()}",
                    sent=sent,
                    sent_ts=datetime.now(tz=timezone.utc) if sent else None,
                )
            )
            row.last_fired_at = ts
            row.fired_count += 1
            fired += 1
            logger.info(
                "price_level_crossed",
                price_level_alert_id=row.id,
                asset_id=asset_id,
                direction=row.direction,
                level=float(row.level),
                price=float(price),
                sent=sent,
            )
        row.last_price = price

    await session.commit()
    return fired
