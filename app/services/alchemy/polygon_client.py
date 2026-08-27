"""Alchemy Polygon RPC client — optional fallback for wallet attribution. Phase 5."""
from app.config import get_settings
from app.logging import get_logger

logger = get_logger(__name__)


async def get_transaction(tx_hash: str) -> dict | None:
    """Fetch a transaction from the Polygon chain. Stub — Phase 5."""
    settings = get_settings()
    if not settings.enable_alchemy_reconciliation:
        return None
    raise NotImplementedError("Alchemy reconciliation not yet implemented (Phase 5)")
