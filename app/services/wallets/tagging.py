"""Manual wallet tagging helpers. Phase 3."""
from app.logging import get_logger

logger = get_logger(__name__)

VALID_TAGS = {
    "watch", "ignore", "high-interest", "suspected-hedge", "market-maker", "arb",
    # Manual badge overrides (in addition to system-assigned badges)
    "informed", "farmer", "sybil",
}
VALID_WATCH_STATUSES = {"watch", "ignore", "high-interest"}
