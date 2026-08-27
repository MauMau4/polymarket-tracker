"""Tests for the symptom-based freshness watchdog (decisions/2026-07-18.md)."""
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from app.db.models import Market, Trade
from app.tasks.run_freshness_watchdog import _compute_freshness


def _market(market_id: str, end_date: datetime | None, resolution_checked_at: datetime | None) -> Market:
    now = datetime.now(tz=timezone.utc)
    m = Market(
        market_id=market_id, slug=market_id, question=market_id, active=True, closed=False,
        resolved=False, end_date=end_date,
    )
    m.resolution_checked_at = resolution_checked_at
    m.updated_at = now
    return m


def _trade(ts: datetime, market_id: str = "m1") -> Trade:
    return Trade(
        ts=ts, market_id=market_id, asset_id="a1", price=Decimal("0.5"), size=Decimal("10"),
        notional_usd=Decimal("5"), side="BUY", source="websocket",
    )


async def test_all_healthy_reports_ok(db_session):
    now = datetime.now(tz=timezone.utc)
    db_session.add(_market("m1", end_date=None, resolution_checked_at=now))
    db_session.add(_trade(now))
    await db_session.commit()

    result = await _compute_freshness(db_session)

    assert result["ok"] is True
    assert result["problems"] == []


async def test_no_trades_at_all_is_a_problem(db_session):
    now = datetime.now(tz=timezone.utc)
    db_session.add(_market("m1", end_date=None, resolution_checked_at=now))
    await db_session.commit()

    result = await _compute_freshness(db_session)

    assert result["ok"] is False
    assert any("trades table is completely empty" in p for p in result["problems"])


async def test_stale_trades_flagged(db_session):
    now = datetime.now(tz=timezone.utc)
    db_session.add(_market("m1", end_date=None, resolution_checked_at=now))
    db_session.add(_trade(now - timedelta(hours=5)))
    await db_session.commit()

    result = await _compute_freshness(db_session)

    assert result["ok"] is False
    assert any("no trades ingested" in p for p in result["problems"])


async def test_resolver_stalled_with_candidates_waiting_is_a_problem(db_session):
    now = datetime.now(tz=timezone.utc)
    # A market past its end_date, unresolved, whose resolution_checked_at is
    # stale — a real backlog the resolver isn't clearing.
    db_session.add(_market("m1", end_date=now - timedelta(days=1), resolution_checked_at=now - timedelta(hours=5)))
    db_session.add(_trade(now))
    await db_session.commit()

    result = await _compute_freshness(db_session)

    assert result["ok"] is False
    assert any("resolver made no progress" in p for p in result["problems"])
    assert result["resolver_candidates_waiting"] == 1


async def test_resolver_candidates_but_recently_checked_is_fine(db_session):
    now = datetime.now(tz=timezone.utc)
    db_session.add(_market("m1", end_date=now - timedelta(days=1), resolution_checked_at=now - timedelta(minutes=5)))
    db_session.add(_trade(now))
    await db_session.commit()

    result = await _compute_freshness(db_session)

    assert result["ok"] is True
    assert result["resolver_candidates_waiting"] == 1
