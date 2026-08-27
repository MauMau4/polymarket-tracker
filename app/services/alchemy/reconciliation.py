"""Alchemy-based wallet attribution fallback. Phase 5."""
from app.logging import get_logger

logger = get_logger(__name__)


async def reconcile_unresolved_trades() -> int:
    """Attempt Alchemy attribution for unresolved trades. Stub — Phase 5."""
    raise NotImplementedError("Alchemy reconciliation not yet implemented (Phase 5)")
