"""Management API for generic price-level crossing alerts
(decisions/2026-07-19.md item 3, C-NFL prerequisite)."""
from decimal import Decimal

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import db_session
from app.db.models import PriceLevelAlert
from app.logging import get_logger

router = APIRouter(prefix="/price-level-alerts", tags=["price-level-alerts"])
logger = get_logger(__name__)


class CreatePriceLevelAlertRequest(BaseModel):
    market_id: str
    asset_id: str
    level: float
    direction: str = Field(pattern="^(above|below)$")
    label: str | None = None
    cooldown_seconds: int = 300


def _serialize(row: PriceLevelAlert) -> dict:
    return {
        "id": row.id,
        "market_id": row.market_id,
        "asset_id": row.asset_id,
        "label": row.label,
        "level": float(row.level),
        "direction": row.direction,
        "cooldown_seconds": row.cooldown_seconds,
        "active": row.active,
        "last_price": float(row.last_price) if row.last_price is not None else None,
        "last_fired_at": row.last_fired_at.isoformat() if row.last_fired_at else None,
        "fired_count": row.fired_count,
        "created_at": row.created_at.isoformat(),
    }


@router.get("")
async def list_price_level_alerts(session: AsyncSession = Depends(db_session)) -> list[dict]:
    result = await session.execute(select(PriceLevelAlert).order_by(PriceLevelAlert.created_at.desc()))
    return [_serialize(r) for r in result.scalars().all()]


@router.post("")
async def create_price_level_alert(
    body: CreatePriceLevelAlertRequest,
    session: AsyncSession = Depends(db_session),
) -> dict:
    row = PriceLevelAlert(
        market_id=body.market_id,
        asset_id=body.asset_id,
        label=body.label,
        level=Decimal(str(body.level)),
        direction=body.direction,
        cooldown_seconds=body.cooldown_seconds,
    )
    session.add(row)
    await session.commit()
    logger.info(
        "price_level_alert_created",
        id=row.id,
        asset_id=body.asset_id,
        level=body.level,
        direction=body.direction,
    )
    return _serialize(row)


@router.delete("/{alert_id}")
async def delete_price_level_alert(
    alert_id: str,
    session: AsyncSession = Depends(db_session),
) -> dict:
    result = await session.execute(select(PriceLevelAlert).where(PriceLevelAlert.id == alert_id))
    row = result.scalar_one_or_none()
    if row is None:
        raise HTTPException(status_code=404, detail="Price level alert not found")
    await session.delete(row)
    await session.commit()
    logger.info("price_level_alert_deleted", id=alert_id)
    return {"deleted": alert_id}
