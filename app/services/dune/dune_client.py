"""Dune Analytics API client. Optional — Phase 5."""
from app.config import get_settings
from app.logging import get_logger

logger = get_logger(__name__)


async def run_query(query_id: int, parameters: dict | None = None) -> list[dict]:
    """Execute a Dune query and return results. Stub — Phase 5."""
    settings = get_settings()
    if not settings.enable_dune_backfill:
        logger.debug("dune_disabled")
        return []
    raise NotImplementedError("Dune client not yet implemented (Phase 5)")
