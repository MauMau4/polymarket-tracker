"""Telegram Bot API integration — one-way message delivery via httpx."""
import httpx
from tenacity import (
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

from app.config import get_settings
from app.logging import get_logger

logger = get_logger(__name__)

_TELEGRAM_API_BASE = "https://api.telegram.org"


@retry(
    retry=retry_if_exception_type((httpx.HTTPStatusError, httpx.TransportError)),
    wait=wait_exponential(multiplier=1, min=1, max=60),
    stop=stop_after_attempt(5),
    reraise=True,
)
async def _post_message(token: str, chat_id: str, text: str) -> None:
    url = f"{_TELEGRAM_API_BASE}/bot{token}/sendMessage"
    payload = {"chat_id": chat_id, "text": text, "parse_mode": "HTML"}
    async with httpx.AsyncClient(timeout=10.0) as client:
        resp = await client.post(url, json=payload)
        if resp.status_code == 429:
            logger.warning("telegram_rate_limited")
            raise httpx.HTTPStatusError("rate limited", request=resp.request, response=resp)
        resp.raise_for_status()


async def send_message(text: str) -> bool:
    """Send text to the configured Telegram chat. Returns True on success."""
    settings = get_settings()
    if not settings.telegram_bot_token or not settings.telegram_chat_id:
        logger.warning("telegram_not_configured")
        return False

    try:
        await _post_message(settings.telegram_bot_token, settings.telegram_chat_id, text)
        logger.info("telegram_message_sent", chat_id=settings.telegram_chat_id)
        return True
    except Exception as exc:
        logger.error("telegram_send_failed", error=str(exc))
        return False
