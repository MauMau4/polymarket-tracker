"""Continuous book-snapshot logger process (architecture §3.5, FR-5).

Docker Compose service `pathfinder-booklog`. Runs take_book_snapshot() every
config.booklog.snapshot_interval_seconds, forever — same crash-restart
pattern as app/tasks/run_ws_ingestor.py, since this needs to survive
unattended for weeks (Gate 4 needs weeks of spread data).

Manual run: python -m pathfinder.booklog.daemon
"""
import asyncio
import sys

from app.db.session import get_session_factory
from app.logging import get_logger, setup_logging
from pathfinder.booklog.snapshotter import take_book_snapshot
from pathfinder.config import get_config

logger = get_logger(__name__)


async def _run_forever() -> None:
    config = get_config()
    session_factory = get_session_factory()
    interval = config.booklog.snapshot_interval_seconds
    loop = asyncio.get_event_loop()

    while True:
        started = loop.time()
        try:
            await take_book_snapshot(config, session_factory)
        except Exception as exc:
            logger.error("book_snapshot_cycle_failed", error=str(exc), exc_info=True)
        elapsed = loop.time() - started
        await asyncio.sleep(max(0.0, interval - elapsed))


async def main() -> None:
    setup_logging()
    logger.info("pathfinder_booklog_started")

    restart_delay = 5.0
    while True:
        try:
            await _run_forever()
            logger.warning("book_snapshot_loop_exited_unexpectedly", restart_in=restart_delay)
        except (KeyboardInterrupt, SystemExit):
            logger.info("pathfinder_booklog_shutdown_requested")
            break
        except Exception as exc:
            logger.error(
                "pathfinder_booklog_crashed", error=str(exc), restart_in=restart_delay, exc_info=True
            )

        await asyncio.sleep(restart_delay)
        restart_delay = min(restart_delay * 2, 60.0)


if __name__ == "__main__":
    if sys.platform == "win32":
        asyncio.run(main(), loop_factory=asyncio.SelectorEventLoop)
    else:
        asyncio.run(main())
