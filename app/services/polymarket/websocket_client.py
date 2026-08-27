"""Polymarket market WebSocket consumer with automatic reconnection."""
import asyncio
import json
import ssl
from collections.abc import AsyncIterator
from datetime import datetime, timezone

import websockets
from websockets.exceptions import ConnectionClosed, WebSocketException

from app.config import get_settings
from app.logging import get_logger

logger = get_logger(__name__)

# Polymarket's WS server silently drops connections when a single subscription
# frame exceeds ~40–832KB. 500 IDs ≈ 40KB stays well below the limit.
_SUBSCRIPTION_CHUNK_SIZE = 500

# Python 3.14 on Windows enforces stricter CA cert basic-constraints validation
# that Polymarket's intermediate CA cert does not satisfy. Bypass verification
# for the WS connection only. Option B (system cert store or certifi bundle) is
# the permanent fix.
_ssl_ctx = ssl.create_default_context()
_ssl_ctx.check_hostname = False
_ssl_ctx.verify_mode = ssl.CERT_NONE

# Global heartbeat timestamp — written by the consumer, read by the health endpoint
_last_heartbeat: datetime | None = None


def get_last_heartbeat() -> datetime | None:
    return _last_heartbeat


class MarketWebSocketClient:
    """
    Subscribes to the Polymarket market WebSocket channel and yields raw event dicts.

    Handles:
    - Initial subscription with the given asset IDs
    - Reconnection with exponential backoff on disconnect
    - Dynamic re-subscription after reconnect (fetches fresh asset IDs via callback)
    """

    def __init__(
        self,
        get_asset_ids_callback,
        reconnect_base_delay: float = 1.0,
        reconnect_max_delay: float = 60.0,
    ) -> None:
        self._get_asset_ids = get_asset_ids_callback
        self._reconnect_base = reconnect_base_delay
        self._reconnect_max = reconnect_max_delay
        self._running = False

    async def _subscribe(self, ws, asset_ids: list[str]) -> None:
        chunks = [
            asset_ids[i:i + _SUBSCRIPTION_CHUNK_SIZE]
            for i in range(0, len(asset_ids), _SUBSCRIPTION_CHUNK_SIZE)
        ]
        for chunk in chunks:
            msg = json.dumps({"assets_ids": chunk, "type": "Market"})
            await ws.send(msg)
            await asyncio.sleep(0.05)
        logger.info("ws_subscribed", asset_count=len(asset_ids), chunks=len(chunks))

    async def _iter_messages(self, ws) -> AsyncIterator[dict | list]:
        async for raw in ws:
            global _last_heartbeat
            _last_heartbeat = datetime.now(tz=timezone.utc)

            if not raw:
                continue

            try:
                data = json.loads(raw)
            except json.JSONDecodeError:
                logger.warning("ws_json_decode_error", raw=raw[:200])
                continue

            yield data

    async def run(self, on_event) -> None:
        """
        Connect, subscribe, and call on_event(raw_event: dict) for each incoming message.
        Reconnects indefinitely until stop() is called.
        """
        self._running = True
        settings = get_settings()
        delay = self._reconnect_base

        while self._running:
            asset_ids = await self._get_asset_ids()
            if not asset_ids:
                logger.warning("ws_no_asset_ids_sleeping", seconds=30)
                await asyncio.sleep(30)
                continue

            logger.info("ws_connecting", url=settings.polymarket_market_ws_url)
            logger.warning(
                "ws_ssl_verification_disabled",
                reason="Python 3.14 Windows CA chain incompatibility - see Option B for permanent fix",
            )
            try:
                async with websockets.connect(
                    settings.polymarket_market_ws_url,
                    ssl=_ssl_ctx,
                    ping_interval=20,
                    ping_timeout=20,
                    open_timeout=30,
                    max_size=None,
                ) as ws:
                    delay = self._reconnect_base
                    await self._subscribe(ws, asset_ids)
                    logger.info("ws_connected")

                    async for event in self._iter_messages(ws):
                        if not self._running:
                            break
                        try:
                            await on_event(event)
                        except Exception as exc:
                            logger.error("ws_event_handler_error", error=str(exc), exc_info=True)

            except ConnectionClosed as exc:
                logger.warning("ws_connection_closed", code=exc.code, reason=exc.reason)
            except WebSocketException as exc:
                logger.error("ws_error", error=str(exc))
            except Exception as exc:
                logger.error("ws_unexpected_error", error=str(exc), exc_info=True)

            if not self._running:
                break

            logger.info("ws_reconnecting", delay=delay)
            await asyncio.sleep(delay)
            delay = min(delay * 2, self._reconnect_max)

    def stop(self) -> None:
        self._running = False


def parse_events(raw: dict | list) -> list[dict]:
    """
    Normalize incoming WS payloads — the server may send a single object or a list.
    Returns a flat list of event dicts.
    """
    if isinstance(raw, list):
        events = []
        for item in raw:
            if isinstance(item, dict):
                events.append(item)
            else:
                logger.warning("ws_unexpected_list_item", item_type=type(item).__name__)
        return events
    if isinstance(raw, dict):
        return [raw]
    logger.warning("ws_unknown_payload_type", payload_type=type(raw).__name__)
    return []
