"""
System/operational alert routing — CRIT/WARN/INFO severity tiers for
infrastructure health signals (Gamma canary, freshness watchdog, scheduler
liveness, resolver failures, epsilon-close failures). Separate from the
trade-alert engine's whale/watched-wallet severities (app/services/alerts
/engine.py's "watch"/"low"/"medium"/"none" scale, which means something
different — trade significance, not system health — and is untouched here).

decisions/2026-07-18.md:
  CRIT -> immediate Telegram push, visually distinct from whale/wallet
          alerts so it can't be mistaken for one.
  WARN -> queued in Redis, flushed once/day as a single digest message.
          Nothing is sent if the queue is empty — no daily heartbeat.
  INFO -> logging only; this module has no action for INFO, the existing
          structlog logger.info(...) calls at each call site already are
          the INFO tier.

No new infrastructure: reuses the existing Telegram sender
(app/services/alerts/telegram.py) and Redis (already used for alert-engine
dedup, app/services/alerts/engine.py).
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Literal

import redis.asyncio as redis_async

from app.config import get_settings
from app.logging import get_logger
from app.services.alerts import telegram

logger = get_logger(__name__)

Severity = Literal["CRIT", "WARN", "INFO"]

_WARN_QUEUE_KEY = "system_alerts:warn_queue"
_CRIT_PREFIX = "🚨 SYSTEM"
_WARN_PREFIX = "⚠️ SYSTEM (daily digest)"


def _get_redis() -> redis_async.Redis:
    return redis_async.from_url(get_settings().redis_url)


async def send_system_alert(severity: Severity, source: str, message: str) -> None:
    """Route one system/operational alert by severity. Never raises."""
    if severity == "CRIT":
        text = f"{_CRIT_PREFIX}: [{source}] {message}"
        sent = await telegram.send_message(text)
        logger.info("system_alert_crit_dispatched", source=source, sent=sent)
        return

    if severity == "WARN":
        entry = json.dumps({
            "source": source,
            "message": message,
            "ts": datetime.now(tz=timezone.utc).isoformat(),
        })
        r = _get_redis()
        try:
            await r.rpush(_WARN_QUEUE_KEY, entry)
        except Exception as exc:
            logger.warning("system_alert_warn_queue_error", error=str(exc))
        finally:
            await r.aclose()
        return

    # INFO: the call site's own logger.info(...) already covers this tier.


async def flush_warn_digest() -> dict:
    """Drains the WARN queue and sends one batched Telegram message if
    non-empty. Called once/day (job_warn_digest, run_worker.py). Sends
    nothing at all if the queue is empty — deliberately no heartbeat."""
    r = _get_redis()
    try:
        entries_raw = await r.lrange(_WARN_QUEUE_KEY, 0, -1)
        if entries_raw:
            await r.delete(_WARN_QUEUE_KEY)
    except Exception as exc:
        logger.warning("system_alert_warn_flush_error", error=str(exc))
        return {"sent": False, "count": 0}
    finally:
        await r.aclose()

    if not entries_raw:
        return {"sent": False, "count": 0}

    entries = [json.loads(e) for e in entries_raw]
    lines = [f"- [{e['source']}] {e['message']}" for e in entries]
    plural = "s" if len(entries) != 1 else ""
    text = f"{_WARN_PREFIX} ({len(entries)} item{plural}):\n" + "\n".join(lines)
    sent = await telegram.send_message(text)
    logger.info("system_alert_warn_digest_sent", count=len(entries), sent=sent)
    return {"sent": sent, "count": len(entries)}
