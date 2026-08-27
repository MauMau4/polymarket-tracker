"""
Cross-process scheduler liveness check.

The problem this solves: every other watchdog in this codebase (Gamma canary,
freshness watchdog) is ITSELF a job scheduled by `run_worker.py`'s
APScheduler. If the scheduler process dies outright, none of those jobs run
either — nothing can self-report its own scheduler's death. This is exactly
what happened for months before 2026-07-17 (decisions/2026-07-17.md,
"worker service never ran the actual job scheduler").

Fix: two independent long-running processes already exist in this codebase
(`run_worker.py` and `run_ws_ingestor.py`, per architecture — they're
deliberately isolated). The worker writes a heartbeat to Redis every 5
minutes (`record_heartbeat`, scheduled as `job_scheduler_liveness`); the
INGESTOR — a separate process, so it keeps running even if the worker dies —
checks that heartbeat's age on its own 5-minute subscription-refresh cycle
(`check_scheduler_liveness`, called from `run_ws_ingestor.py`). If the
heartbeat is missing or stale, the scheduler is dead and the ingestor is the
only thing left alive to say so.

No new infrastructure: reuses Redis, already used elsewhere in this codebase
(app/services/alerts/engine.py's dedupe logic) via the same access pattern.
"""
from datetime import datetime, timezone

import redis.asyncio as redis_async

from app.config import get_settings
from app.logging import get_logger

logger = get_logger(__name__)

_HEARTBEAT_KEY = "scheduler:heartbeat"
_HEARTBEAT_TTL_SECONDS = 1800  # 30 min — outlives the stale threshold so a
# late-but-present heartbeat is distinguishable from a fully-expired one
_STALE_THRESHOLD_SECONDS = 900  # 15 min — 3x the 5-min write cadence

# decisions/2026-07-23.md: a Redis-read failure alone must NOT CRIT (tolerate
# a transient blip, the original intent) but sustained Redis-down for the
# same duration as the heartbeat-staleness threshold above is itself the
# signal that this whole check has gone blind and needs to page. Redis can't
# be used to track how long Redis has been down, so this is in-process state
# on the long-lived ingestor — reset on any successful Redis read.
_REDIS_DOWN_CRIT_THRESHOLD_SECONDS = 900  # 15 min, same order as heartbeat staleness
_redis_down_since: datetime | None = None
_redis_down_crit_sent = False  # de-dupe: CRIT once per outage, on threshold crossing only


def _get_redis() -> redis_async.Redis:
    return redis_async.from_url(get_settings().redis_url)


async def record_heartbeat() -> None:
    """Called by the worker scheduler every 5 minutes."""
    r = _get_redis()
    try:
        await r.set(_HEARTBEAT_KEY, datetime.now(tz=timezone.utc).isoformat(), ex=_HEARTBEAT_TTL_SECONDS)
    except Exception as exc:
        logger.error("scheduler_heartbeat_write_error", error=str(exc))
    finally:
        await r.aclose()


async def check_scheduler_liveness() -> dict:
    """Called by the ingestor (a separate process) on its own refresh cycle.
    A single transient Redis-read failure is not treated as scheduler death
    (never raises, never CRITs) — but sustained Redis-down past
    _REDIS_DOWN_CRIT_THRESHOLD_SECONDS escalates to CRIT once (de-duped until
    Redis recovers), since at that point this check itself has gone blind."""
    global _redis_down_since, _redis_down_crit_sent

    r = _get_redis()
    try:
        raw = await r.get(_HEARTBEAT_KEY)
    except Exception as exc:
        now = datetime.now(tz=timezone.utc)
        if _redis_down_since is None:
            _redis_down_since = now
        down_seconds = (now - _redis_down_since).total_seconds()
        logger.warning(
            "scheduler_liveness_check_redis_error",
            error=str(exc),
            down_seconds=round(down_seconds, 1),
        )
        if down_seconds >= _REDIS_DOWN_CRIT_THRESHOLD_SECONDS and not _redis_down_crit_sent:
            from app.services.alerts.system_alerts import send_system_alert
            await send_system_alert(
                "CRIT", "scheduler_liveness",
                f"Redis unavailable for {down_seconds:.0f}s — scheduler-liveness heartbeat "
                "check cannot run (and WARN alerts are not being delivered either, since the "
                "WARN queue is also Redis-backed).",
            )
            _redis_down_crit_sent = True
        return {
            "ok": False,
            "checked": False,
            "reason": f"redis unavailable: {exc}",
            "down_seconds": round(down_seconds, 1),
        }
    finally:
        await r.aclose()

    # Redis read succeeded — connectivity is back (or was never down).
    _redis_down_since = None
    _redis_down_crit_sent = False

    from app.services.alerts.system_alerts import send_system_alert

    if raw is None:
        logger.critical(
            "scheduler_dead_no_heartbeat",
            heartbeat_key=_HEARTBEAT_KEY,
            stale_threshold_seconds=_STALE_THRESHOLD_SECONDS,
        )
        await send_system_alert(
            "CRIT", "scheduler_liveness",
            "No scheduler heartbeat found — worker process may be dead (no scheduled jobs are running).",
        )
        return {"ok": False, "checked": True, "reason": "no heartbeat found (missing or TTL-expired)"}

    last_beat = datetime.fromisoformat(raw.decode() if isinstance(raw, bytes) else raw)
    age_seconds = (datetime.now(tz=timezone.utc) - last_beat).total_seconds()
    if age_seconds >= _STALE_THRESHOLD_SECONDS:
        logger.critical(
            "scheduler_dead_stale_heartbeat",
            last_beat=last_beat.isoformat(),
            age_seconds=round(age_seconds, 1),
        )
        await send_system_alert(
            "CRIT", "scheduler_liveness",
            f"Scheduler heartbeat stale by {age_seconds:.0f}s — worker process may be dead.",
        )
        return {"ok": False, "checked": True, "reason": f"heartbeat stale by {age_seconds:.0f}s"}

    return {"ok": True, "checked": True, "age_seconds": round(age_seconds, 1)}
