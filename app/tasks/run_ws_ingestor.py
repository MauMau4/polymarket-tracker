"""
WebSocket ingestor process — entry point for the real-time ingestion pipeline.

Runs continuously:
1. Initial market discovery to populate DB and get active asset IDs
2. Start WebSocket consumer subscribed to all active assets
3. Route each event through the trade normalizer → attribution pipeline → DB write
4. Periodically refresh the asset subscription list as markets open/close

This process is intentionally isolated from the scheduler worker (run_worker.py).
"""
import asyncio
import sys
from app.logging import setup_logging, get_logger
from app.services.discovery.refresh import run_discovery, run_sports_discovery
from app.services.polymarket.normalizer import TradeNormalizer
from app.services.polymarket.websocket_client import MarketWebSocketClient

logger = get_logger(__name__)

# How often to refresh the asset subscription list (seconds)
_SUBSCRIPTION_REFRESH_INTERVAL = 300


class Ingestor:
    def __init__(self) -> None:
        self._normalizer = TradeNormalizer()
        self._asset_ids: list[str] = []
        self._running = False

    async def _refresh_subscriptions(self) -> list[str]:
        """Run general discovery to keep DB fresh, then build subscription list from DB.

        Asset IDs are sourced from the DB (not from the discovery return value) so that
        sports markets added by run_sports_discovery() are included in WS subscriptions.
        """
        try:
            await run_discovery()
        except Exception as exc:
            logger.error("ingestor_discovery_failed", error=str(exc))

        from sqlalchemy import select
        from app.config import get_settings
        from app.db.models import Token, Market, Wallet
        from app.db.session import get_session_factory
        factory = get_session_factory()
        settings = get_settings()
        asset_ids: list[str] = []
        async with factory() as session:
            if settings.enable_volume_ranked_subscription:
                from app.services.discovery.refresh import get_subscription_asset_ids
                selected_asset_ids, diagnostics = await get_subscription_asset_ids(
                    session, top_n=settings.subscription_top_n
                )
                selected_set = set(selected_asset_ids)
                rows = await session.execute(
                    select(Token.asset_id, Token.market_id, Token.outcome, Market.end_date)
                    .join(Market, Token.market_id == Market.market_id)
                    .where(Token.asset_id.in_(selected_set))
                )
                logger.info("ingestor_volume_ranked_subscription", **diagnostics)
            else:
                rows = await session.execute(
                    select(Token.asset_id, Token.market_id, Token.outcome, Market.end_date)
                    .join(Market, Token.market_id == Market.market_id)
                    .where(Market.active == True, Market.resolved == False,  # noqa: E712
                       Market.closed == False)
                )
            for asset_id, market_id, outcome, end_date in rows:
                self._normalizer.update_asset_map(asset_id, market_id)
                self._normalizer.update_asset_outcome(asset_id, outcome)
                self._normalizer.update_market_end_date(market_id, end_date)
                asset_ids.append(asset_id)

            watched_rows = await session.execute(
                select(Wallet.wallet).where(Wallet.watch_status == "watch")
            )
            watched = {row[0] for row in watched_rows}
            self._normalizer.load_watched_wallets(watched)

        self._asset_ids = list(dict.fromkeys(asset_ids))  # deduplicate, preserve order
        logger.info("ingestor_subscriptions_refreshed", asset_count=len(self._asset_ids))
        return self._asset_ids

    async def _get_asset_ids(self) -> list[str]:
        """Callback used by the WebSocket client to (re)subscribe after reconnect."""
        if not self._asset_ids:
            await self._refresh_subscriptions()
        return self._asset_ids

    async def _subscription_refresh_loop(self) -> None:
        """Background loop that periodically refreshes market subscriptions.

        Also piggybacks the scheduler-liveness check (decisions/2026-07-18.md)
        on this same cadence: the ingestor is a separate process from the
        worker scheduler, so it's the one thing still alive to notice if the
        scheduler dies — the previous incident (worker sat dead for months,
        decisions/2026-07-17.md) had no such cross-process check.
        """
        await asyncio.sleep(_SUBSCRIPTION_REFRESH_INTERVAL)
        while self._running:
            await self._refresh_subscriptions()
            try:
                from app.tasks.run_scheduler_liveness_check import check_scheduler_liveness
                await check_scheduler_liveness()
            except Exception as exc:
                logger.error("ingestor_scheduler_liveness_check_failed", error=str(exc))
            await asyncio.sleep(_SUBSCRIPTION_REFRESH_INTERVAL)

    async def _on_event(self, raw) -> None:
        await self._normalizer.process_ws_payload(raw)

    async def run(self) -> None:
        self._running = True
        logger.info("ingestor_starting")

        # Warm up: run sports discovery first so sports markets are in the DB
        # before _refresh_subscriptions() builds the subscription list from DB.
        try:
            await run_sports_discovery()
        except Exception as exc:
            logger.error("ingestor_sports_discovery_failed", error=str(exc))

        await self._refresh_subscriptions()

        ws_client = MarketWebSocketClient(get_asset_ids_callback=self._get_asset_ids)

        refresh_task = asyncio.create_task(self._subscription_refresh_loop())

        try:
            await ws_client.run(on_event=self._on_event)
        finally:
            self._running = False
            refresh_task.cancel()
            try:
                await refresh_task
            except asyncio.CancelledError:
                pass
            logger.info("ingestor_stopped")


async def main() -> None:
    setup_logging()
    logger.info("run_ws_ingestor_process_started")

    restart_delay = 5.0
    while True:
        try:
            ingestor = Ingestor()
            await ingestor.run()
            # run() should loop forever; a clean return is unexpected
            logger.warning("ingestor_exited_unexpectedly", restart_in=restart_delay)
        except (KeyboardInterrupt, SystemExit):
            logger.info("ingestor_shutdown_requested")
            break
        except Exception as exc:
            logger.error(
                "ingestor_crashed",
                error=str(exc),
                restart_in=restart_delay,
                exc_info=True,
            )

        await asyncio.sleep(restart_delay)
        restart_delay = min(restart_delay * 2, 60.0)
        logger.info("ingestor_restarting", delay=restart_delay)


if __name__ == "__main__":
    if sys.platform == "win32":
        asyncio.run(main(), loop_factory=asyncio.SelectorEventLoop)
    else:
        asyncio.run(main())
