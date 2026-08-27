"""
One-shot: purge wallet_score_history rows older than 7 days.

The scheduled job (daily at 02:15 UTC) calls the same purge_old_score_history()
function going forward. Run this once to clear rows that accumulated before the
job was active.

Run: python -m app.tasks.run_score_history_cleanup
"""
import asyncio
import selectors
import sys
from sqlalchemy import text

from app.db.session import get_session_factory
from app.logging import setup_logging, get_logger
from app.services.maintenance.retention import purge_old_score_history

logger = get_logger(__name__)


async def run() -> None:
    purged = await purge_old_score_history()
    print(f"Purged {purged} score history rows older than 7 days.")

    factory = get_session_factory()
    async with factory() as session:
        result = await session.execute(
            text("SELECT COUNT(*) FROM wallet_score_history WHERE snapshot_ts < NOW() - INTERVAL '7 days'")
        )
        count = result.scalar_one()
    print(f"Remaining rows older than 7 days: {count}")
    if count == 0:
        print("Verification passed — no stale rows remain.")
    else:
        print(f"WARNING: {count} stale rows still remain.")


async def main() -> None:
    setup_logging()
    await run()


if __name__ == "__main__":
    if sys.platform == "win32":
        asyncio.run(main(), loop_factory=lambda: asyncio.SelectorEventLoop(selectors.SelectSelector()))
    else:
        asyncio.run(main())
