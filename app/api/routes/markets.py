from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import db_session
from app.db.models import Alert, Market, Trade

router = APIRouter(prefix="/markets", tags=["markets"])


@router.get("")
async def list_markets(
    active: bool | None = Query(None),
    status: str | None = Query(None),
    search: str | None = Query(None),
    category: str | None = Query(None),
    session: AsyncSession = Depends(db_session),
) -> list[dict]:
    from sqlalchemy import or_
    stmt = select(Market)
    # Status filter: explicit `status` param takes precedence over legacy `active` bool
    if status == "resolved":
        stmt = stmt.where(Market.resolved == True)  # noqa: E712
    elif status == "closed":
        stmt = stmt.where(Market.closed == True, Market.resolved == False)  # noqa: E712
    elif status == "all":
        pass
    elif active is not None:
        stmt = stmt.where(Market.active == active)
    else:
        stmt = stmt.where(Market.active == True)  # noqa: E712

    if search:
        stmt = stmt.where(
            or_(Market.question.ilike(f"%{search}%"), Market.event_title.ilike(f"%{search}%"))
        )
    if category:
        stmt = stmt.where(Market.category == category)

    stmt = stmt.order_by(
        Market.active.desc(),
        Market.end_date.asc().nulls_last(),
        Market.market_id.asc(),
    ).limit(200)

    result = await session.execute(stmt)
    markets = result.scalars().all()
    return [
        {
            "market_id": m.market_id,
            "question": m.question,
            "active": m.active,
            "closed": m.closed,
            "resolved": m.resolved,
            "category": m.category,
            "end_date": m.end_date.isoformat() if m.end_date else None,
        }
        for m in markets
    ]


@router.get("/{market_id}")
async def get_market(
    market_id: str,
    session: AsyncSession = Depends(db_session),
) -> dict:
    result = await session.execute(select(Market).where(Market.market_id == market_id))
    market = result.scalar_one_or_none()
    if market is None:
        raise HTTPException(status_code=404, detail="Market not found")

    trades_result = await session.execute(
        select(Trade).where(Trade.market_id == market_id).order_by(Trade.ts.desc()).limit(50)
    )
    trades = trades_result.scalars().all()

    alerts_result = await session.execute(
        select(Alert)
        .where(Alert.market_id == market_id)
        .order_by(Alert.created_at.desc())
        .limit(20)
    )
    alerts = alerts_result.scalars().all()
    return {
        "market": {
            "market_id": market.market_id,
            "question": market.question,
            "active": market.active,
            "closed": market.closed,
            "resolved": market.resolved,
            "resolution": market.resolution,
            "category": market.category,
            "end_date": market.end_date.isoformat() if market.end_date else None,
        },
        "recent_trades": [
            {
                "id": t.id,
                "ts": t.ts.isoformat(),
                "wallet": t.wallet,
                "price": float(t.price) if t.price else None,
                "size": float(t.size) if t.size else None,
                "notional_usd": float(t.notional_usd) if t.notional_usd else None,
                "side": t.side,
                "outcome": t.outcome,
                "attribution_status": t.attribution_status.value,
            }
            for t in trades
        ],
        "recent_alerts": [
            {
                "id": a.id,
                "alert_type": a.alert_type,
                "severity": a.severity,
                "wallet": a.wallet,
                "title": a.title,
                "sent": a.sent,
                "created_at": a.created_at.isoformat(),
            }
            for a in alerts
        ],
    }
