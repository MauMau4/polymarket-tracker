"""Alchemy reconciliation task for unresolved trades. Phase 5 optional."""
import asyncio
import sys
from app.logging import setup_logging, get_logger

logger = get_logger(__name__)


async def main() -> None:
    setup_logging()
    raise NotImplementedError("Alchemy reconciliation not yet implemented (Phase 5)")


if __name__ == "__main__":
    if sys.platform == "win32":
        asyncio.run(main(), loop_factory=asyncio.SelectorEventLoop)
    else:
        asyncio.run(main())
