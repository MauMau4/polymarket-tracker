"""Wallet score recomputation task — runs on a schedule (daily at 00:00 UTC)."""
import asyncio
import sys

from sqlalchemy import select

from app.db.models.wallet import Wallet
from app.db.session import get_session_factory
from app.logging import setup_logging, get_logger
from app.services.wallets import scorer

logger = get_logger(__name__)


async def refresh_all_scores() -> None:
    """
    Tag farming trades for all wallets, then run a single batch recomputation
    of all wallet scores from wallet_closed_positions.
    """
    factory = get_session_factory()
    async with factory() as session:
        result = await session.execute(select(Wallet.wallet))
        wallet_addresses = [row[0] for row in result.all()]

    logger.info("score_refresh_starting", wallet_count=len(wallet_addresses))

    # Tag farming trades per wallet (feeds FARMER badge)
    tag_errors = 0
    for address in wallet_addresses:
        try:
            factory = get_session_factory()
            async with factory() as session:
                tagged = await scorer.tag_farming_trades(session, address)
                await session.commit()
            if tagged:
                logger.debug("farming_tagged", wallet=address, count=tagged)
        except Exception as exc:
            logger.error("farming_tag_error", wallet=address, error=str(exc))
            tag_errors += 1

    # Single batch recomputation (normalization requires all wallets at once)
    try:
        factory = get_session_factory()
        async with factory() as session:
            score_result = await scorer.run_all_wallet_scores(session)
            await session.commit()
        wallets_scored = score_result.get("wallets_scored", 0)
    except Exception as exc:
        logger.error("score_refresh_batch_error", error=str(exc))
        wallets_scored = 0

    logger.info(
        "score_refresh_complete",
        wallets_scored=wallets_scored,
        tag_errors=tag_errors,
        total=len(wallet_addresses),
    )


async def main() -> None:
    setup_logging()
    await refresh_all_scores()


if __name__ == "__main__":
    if sys.platform == "win32":
        asyncio.run(main(), loop_factory=asyncio.SelectorEventLoop)
    else:
        asyncio.run(main())
