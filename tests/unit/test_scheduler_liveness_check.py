"""Tests for check_scheduler_liveness's Redis-outage escalation
(decisions/2026-07-23.md issue 1b): a Redis-read failure alone must still
no-op (tolerate a transient blip, the original intent), but sustained
Redis-down past the threshold must escalate to CRIT exactly once per outage
and reset once Redis recovers — this is the check that silently no-op'd for
the entire 2026-07-22 outage."""
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.tasks import run_scheduler_liveness_check as liveness


@pytest.fixture(autouse=True)
def _reset_liveness_state():
    liveness._redis_down_since = None
    liveness._redis_down_crit_sent = False
    yield
    liveness._redis_down_since = None
    liveness._redis_down_crit_sent = False


def _fake_redis(get_side_effect=None, get_return=None):
    client = MagicMock()
    if get_side_effect is not None:
        client.get = AsyncMock(side_effect=get_side_effect)
    else:
        client.get = AsyncMock(return_value=get_return)
    client.aclose = AsyncMock()
    return client


def _recent_heartbeat() -> bytes:
    return datetime.now(tz=timezone.utc).isoformat().encode()


class TestSchedulerLivenessRedisOutageEscalation:
    async def test_single_transient_failure_does_not_crit(self):
        with patch.object(
            liveness.redis_async, "from_url",
            return_value=_fake_redis(get_side_effect=ConnectionError("boom")),
        ), patch(
            "app.services.alerts.system_alerts.send_system_alert", new=AsyncMock()
        ) as mock_alert:
            result = await liveness.check_scheduler_liveness()

        mock_alert.assert_not_awaited()
        assert result["checked"] is False
        assert liveness._redis_down_since is not None
        assert liveness._redis_down_crit_sent is False

    async def test_sustained_failure_past_threshold_crits(self):
        liveness._redis_down_since = datetime.now(tz=timezone.utc) - timedelta(seconds=1000)
        with patch.object(
            liveness.redis_async, "from_url",
            return_value=_fake_redis(get_side_effect=ConnectionError("boom")),
        ), patch(
            "app.services.alerts.system_alerts.send_system_alert", new=AsyncMock()
        ) as mock_alert:
            result = await liveness.check_scheduler_liveness()

        mock_alert.assert_awaited_once()
        severity, source, message = mock_alert.await_args.args
        assert severity == "CRIT"
        assert source == "scheduler_liveness"
        assert "Redis unavailable" in message
        assert liveness._redis_down_crit_sent is True
        assert result["ok"] is False

    async def test_crit_not_repeated_on_subsequent_checks_same_outage(self):
        liveness._redis_down_since = datetime.now(tz=timezone.utc) - timedelta(seconds=1000)
        with patch.object(
            liveness.redis_async, "from_url",
            return_value=_fake_redis(get_side_effect=ConnectionError("boom")),
        ), patch(
            "app.services.alerts.system_alerts.send_system_alert", new=AsyncMock()
        ) as mock_alert:
            await liveness.check_scheduler_liveness()
            await liveness.check_scheduler_liveness()

        assert mock_alert.await_count == 1

    async def test_recovery_resets_state_without_reraising_missing_heartbeat_crit(self):
        liveness._redis_down_since = datetime.now(tz=timezone.utc) - timedelta(seconds=1000)
        liveness._redis_down_crit_sent = True
        with patch.object(
            liveness.redis_async, "from_url",
            return_value=_fake_redis(get_return=_recent_heartbeat()),
        ), patch(
            "app.services.alerts.system_alerts.send_system_alert", new=AsyncMock()
        ) as mock_alert:
            result = await liveness.check_scheduler_liveness()

        assert liveness._redis_down_since is None
        assert liveness._redis_down_crit_sent is False
        assert result["ok"] is True
        mock_alert.assert_not_awaited()

    async def test_new_outage_after_recovery_can_crit_again(self):
        liveness._redis_down_since = datetime.now(tz=timezone.utc) - timedelta(seconds=1000)
        with patch.object(
            liveness.redis_async, "from_url",
            return_value=_fake_redis(get_side_effect=ConnectionError("boom")),
        ), patch(
            "app.services.alerts.system_alerts.send_system_alert", new=AsyncMock()
        ) as mock_alert_1:
            await liveness.check_scheduler_liveness()
        assert mock_alert_1.await_count == 1

        with patch.object(
            liveness.redis_async, "from_url",
            return_value=_fake_redis(get_return=_recent_heartbeat()),
        ), patch(
            "app.services.alerts.system_alerts.send_system_alert", new=AsyncMock()
        ):
            await liveness.check_scheduler_liveness()
        assert liveness._redis_down_since is None
        assert liveness._redis_down_crit_sent is False

        liveness._redis_down_since = datetime.now(tz=timezone.utc) - timedelta(seconds=1000)
        with patch.object(
            liveness.redis_async, "from_url",
            return_value=_fake_redis(get_side_effect=ConnectionError("boom")),
        ), patch(
            "app.services.alerts.system_alerts.send_system_alert", new=AsyncMock()
        ) as mock_alert_2:
            await liveness.check_scheduler_liveness()
        assert mock_alert_2.await_count == 1
