"""One-shot market discovery task — run manually or from the scheduler."""
import asyncio
import sys
from app.logging import setup_logging, get_logger
from app.services.discovery.refresh import run_discovery

logger = get_logger(__name__)


async def main() -> None:
    setup_logging()
    logger.info("run_discovery_starting")
    result = await run_discovery()
    logger.info(
        "run_discovery_complete",
        markets_upserted=result.markets_upserted,
        tokens_upserted=result.tokens_upserted,
        active_assets=len(result.active_asset_ids),
    )


if __name__ == "__main__":
    if sys.platform == "win32":
        asyncio.run(main(), loop_factory=asyncio.SelectorEventLoop)
    else:
        asyncio.run(main())
