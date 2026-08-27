from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import db_session
from app.db.models import Alert, Trade, Wallet, WalletScoreHistory
from app.db.models.position import Position
from app.logging import get_logger
from app.services.wallets.tagging import VALID_TAGS, VALID_WATCH_STATUSES

router = APIRouter(prefix="/wallets", tags=["wallets"])
logger = get_logger(__name__)


class WatchRequest(BaseModel):
    watch_status: str


class AlertDigestRequest(BaseModel):
    enabled: bool


class TagRequest(BaseModel):
    manual_tag: str | None = None
    notes: str | None = None


@router.get("")
async def list_wallets(
    watch_status: str | None = Query(None),
    min_score: float | None = Query(None),
    search: str | None = Query(None),
    session: AsyncSession = Depends(db_session),
) -> list[dict]:
    stmt = select(Wallet).order_by(Wallet.wallet_score.desc()).limit(200)
    if watch_status is not None:
        stmt = stmt.where(Wallet.watch_status == watch_status)
    if min_score is not None:
        stmt = stmt.where(Wallet.wallet_score >= min_score)
    if search:
        stmt = stmt.where(Wallet.wallet.ilike(f"%{search}%"))
    result = await session.execute(stmt)
    wallets = result.scalars().all()
    return [
        {
            "wallet": w.wallet,
            "wallet_score": w.wallet_score,
            "score_confidence": w.score_confidence,
            "watch_status": w.watch_status,
            "alert_digest": w.alert_digest,
            "manual_tag": w.manual_tag,
            "markets_traded_count": w.markets_traded_count,
            "resolved_markets_count": w.resolved_markets_count,
            "last_seen": w.last_seen.isoformat() if w.last_seen else None,
            "first_seen": w.first_seen.isoformat() if w.first_seen else None,
        }
        for w in wallets
    ]


@router.get("/{wallet_address}")
async def get_wallet(
    wallet_address: str,
    session: AsyncSession = Depends(db_session),
) -> dict:
    result = await session.execute(select(Wallet).where(Wallet.wallet == wallet_address))
    wallet = result.scalar_one_or_none()
    if wallet is None:
        raise HTTPException(status_code=404, detail="Wallet not found")

    trades_result = await session.execute(
        select(Trade)
        .where(Trade.wallet == wallet_address)
        .order_by(Trade.ts.desc())
        .limit(50)
    )
    trades = trades_result.scalars().all()

    alerts_result = await session.execute(
        select(Alert)
        .where(Alert.wallet == wallet_address)
        .order_by(Alert.created_at.desc())
        .limit(20)
    )
    alerts = alerts_result.scalars().all()

    positions_result = await session.execute(
        select(Position)
        .where(Position.wallet == wallet_address)
        .order_by(Position.snapshot_ts.desc())
        .limit(20)
    )
    positions = positions_result.scalars().all()

    score_history_result = await session.execute(
        select(WalletScoreHistory)
        .where(WalletScoreHistory.wallet == wallet_address)
        .order_by(WalletScoreHistory.snapshot_ts.desc())
        .limit(30)
    )
    score_history = score_history_result.scalars().all()

    return {
        "wallet": {
            "wallet": wallet.wallet,
            "wallet_score": wallet.wallet_score,
            "score_confidence": wallet.score_confidence,
            "watch_status": wallet.watch_status,
            "alert_digest": wallet.alert_digest,
            "manual_tag": wallet.manual_tag,
            "notes": wallet.notes,
            "markets_traded_count": wallet.markets_traded_count,
            "resolved_markets_count": wallet.resolved_markets_count,
            "estimated_win_rate": wallet.estimated_win_rate,
            "estimated_realized_pnl": wallet.estimated_realized_pnl,
            "early_entry_score": wallet.early_entry_score,
            "concentration_score": wallet.concentration_score,
            "consistency_score": wallet.consistency_score,
            "first_seen": wallet.first_seen.isoformat() if wallet.first_seen else None,
            "last_seen": wallet.last_seen.isoformat() if wallet.last_seen else None,
        },
        "recent_trades": [
            {
                "id": t.id,
                "ts": t.ts.isoformat(),
                "market_id": t.market_id,
                "outcome": t.outcome,
                "side": t.side,
                "price": float(t.price) if t.price else None,
                "notional_usd": float(t.notional_usd) if t.notional_usd else None,
                "attribution_status": t.attribution_status.value,
            }
            for t in trades
        ],
        "recent_alerts": [
            {
                "id": a.id,
                "alert_type": a.alert_type,
                "severity": a.severity,
                "market_id": a.market_id,
                "title": a.title,
                "sent": a.sent,
                "created_at": a.created_at.isoformat(),
            }
            for a in alerts
        ],
        "positions": [
            {
                "asset_id": p.asset_id,
                "market_id": p.market_id,
                "balance": float(p.balance) if p.balance else None,
                "est_cost_basis": float(p.est_cost_basis) if p.est_cost_basis else None,
                "snapshot_ts": p.snapshot_ts.isoformat(),
            }
            for p in positions
        ],
        "score_history": [
            {
                "score": h.score,
                "confidence": h.confidence,
                "snapshot_ts": h.snapshot_ts.isoformat(),
                "components": h.components,
            }
            for h in score_history
        ],
    }


@router.post("/{wallet_address}/watch")
async def set_watch_status(
    wallet_address: str,
    body: WatchRequest,
    session: AsyncSession = Depends(db_session),
) -> dict:
    if body.watch_status not in VALID_WATCH_STATUSES:
        raise HTTPException(
            status_code=422,
            detail=f"Invalid watch_status. Valid values: {sorted(VALID_WATCH_STATUSES)}",
        )
    result = await session.execute(select(Wallet).where(Wallet.wallet == wallet_address))
    wallet = result.scalar_one_or_none()
    if wallet is None:
        raise HTTPException(status_code=404, detail="Wallet not found")
    wallet.watch_status = body.watch_status
    await session.commit()
    logger.info("wallet_watch_status_set", wallet=wallet_address, status=body.watch_status)
    return {"wallet": wallet_address, "watch_status": wallet.watch_status}


@router.delete("/{wallet_address}/watch")
async def clear_watch_status(
    wallet_address: str,
    session: AsyncSession = Depends(db_session),
) -> dict:
    result = await session.execute(select(Wallet).where(Wallet.wallet == wallet_address))
    wallet = result.scalar_one_or_none()
    if wallet is None:
        raise HTTPException(status_code=404, detail="Wallet not found")
    wallet.watch_status = None
    await session.commit()
    logger.info("wallet_watch_status_cleared", wallet=wallet_address)
    return {"wallet": wallet_address, "watch_status": None}


@router.post("/{wallet_address}/alert-digest")
async def set_alert_digest(
    wallet_address: str,
    body: AlertDigestRequest,
    session: AsyncSession = Depends(db_session),
) -> dict:
    """Switch a watched wallet's WATCH_WALLET alerts between real-time pings
    and the once-daily batched digest (decisions/2026-07-19.md item 1b)."""
    result = await session.execute(select(Wallet).where(Wallet.wallet == wallet_address))
    wallet = result.scalar_one_or_none()
    if wallet is None:
        raise HTTPException(status_code=404, detail="Wallet not found")
    wallet.alert_digest = body.enabled
    await session.commit()
    logger.info("wallet_alert_digest_set", wallet=wallet_address, enabled=body.enabled)
    return {"wallet": wallet_address, "alert_digest": wallet.alert_digest}


@router.post("/{wallet_address}/tag")
async def set_tag(
    wallet_address: str,
    body: TagRequest,
    session: AsyncSession = Depends(db_session),
) -> dict:
    if body.manual_tag is not None and body.manual_tag not in VALID_TAGS:
        raise HTTPException(
            status_code=422,
            detail=f"Invalid manual_tag. Valid values: {sorted(VALID_TAGS)}",
        )
    result = await session.execute(select(Wallet).where(Wallet.wallet == wallet_address))
    wallet = result.scalar_one_or_none()
    if wallet is None:
        raise HTTPException(status_code=404, detail="Wallet not found")
    if body.manual_tag is not None:
        wallet.manual_tag = body.manual_tag
    if body.notes is not None:
        wallet.notes = body.notes
    await session.commit()
    logger.info("wallet_tagged", wallet=wallet_address, tag=body.manual_tag)
    return {"wallet": wallet_address, "manual_tag": wallet.manual_tag, "notes": wallet.notes}
