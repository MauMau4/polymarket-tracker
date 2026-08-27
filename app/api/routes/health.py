from datetime import timezone
from fastapi import APIRouter
from sqlalchemy import text
from app.db.session import get_session_factory
from app.logging import get_logger
from app.schemas.api import HealthResponse
from app.services.polymarket.websocket_client import get_last_heartbeat

router = APIRouter()
logger = get_logger(__name__)


@router.get("/health", response_model=HealthResponse, tags=["health"])
async def health() -> HealthResponse:
    db_status = "ok"
    try:
        factory = get_session_factory()
        async with factory() as session:
            await session.execute(text("SELECT 1"))
    except Exception as exc:
        logger.warning("health_db_error", error=str(exc))
        db_status = "error"

    redis_status = "ok"
    try:
        from app.config import get_settings
        import redis.asyncio as redis_async
        r = redis_async.from_url(get_settings().redis_url, socket_connect_timeout=2)
        await r.ping()
        await r.aclose()
    except Exception as exc:
        logger.warning("health_redis_error", error=str(exc))
        redis_status = "error"

    heartbeat = get_last_heartbeat()
    ws_connected = heartbeat is not None

    return HealthResponse(
        status="ok" if db_status == "ok" else "degraded",
        db=db_status,
        redis=redis_status,
        ws_connected=ws_connected,
        last_ws_heartbeat=heartbeat.isoformat() if heartbeat else None,
    )
