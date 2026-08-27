from fastapi import APIRouter, Depends, Query
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import db_session
from app.db.models import Alert

router = APIRouter(prefix="/alerts", tags=["alerts"])


@router.get("")
async def list_alerts(
    severity: str | None = Query(None),
    alert_type: str | None = Query(None),
    market_id: str | None = Query(None),
    wallet: str | None = Query(None),
    session: AsyncSession = Depends(db_session),
) -> list[dict]:
    stmt = select(Alert).order_by(Alert.created_at.desc()).limit(200)
    if severity:
        stmt = stmt.where(Alert.severity == severity)
    if alert_type:
        stmt = stmt.where(Alert.alert_type == alert_type)
    if market_id:
        stmt = stmt.where(Alert.market_id == market_id)
    if wallet:
        stmt = stmt.where(Alert.wallet == wallet)
    result = await session.execute(stmt)
    alerts = result.scalars().all()
    return [
        {
            "id": a.id,
            "alert_type": a.alert_type,
            "severity": a.severity,
            "wallet": a.wallet,
            "market_id": a.market_id,
            "title": a.title,
            "message": a.message,
            "dedupe_key": a.dedupe_key,
            "sent": a.sent,
            "sent_ts": a.sent_ts.isoformat() if a.sent_ts else None,
            "created_at": a.created_at.isoformat(),
        }
        for a in alerts
    ]
