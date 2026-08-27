"""Tests for app/services/scheduling/jobs.py's alert-routing on job crashes
(decisions/2026-07-23.md issue 1a): job_freshness_watchdog's exception path
previously only logged, so a crash (as opposed to a clean run that finds real
problems, which already CRITs on its own inside run_freshness_watchdog) never
reached Telegram — exactly the failure mode that went silent during the
2026-07-22 DB+Redis outage."""
from unittest.mock import AsyncMock, patch

from app.services.scheduling import jobs


class TestJobFreshnessWatchdogCrashAlert:
    async def test_exception_path_sends_crit(self):
        with patch(
            "app.tasks.run_freshness_watchdog.run_freshness_watchdog",
            new=AsyncMock(side_effect=RuntimeError("db down")),
        ), patch(
            "app.services.alerts.system_alerts.send_system_alert", new=AsyncMock()
        ) as mock_alert:
            await jobs.job_freshness_watchdog()

        mock_alert.assert_awaited_once()
        severity, source, message = mock_alert.await_args.args
        assert severity == "CRIT"
        assert source == "freshness_watchdog"
        assert "db down" in message

    async def test_clean_run_does_not_double_alert(self):
        """The wrapper itself must not alert on a clean run — run_freshness_watchdog
        already sends its own CRIT internally when it computes real problems;
        this is unchanged and the wrapper must not duplicate it."""
        with patch(
            "app.tasks.run_freshness_watchdog.run_freshness_watchdog",
            new=AsyncMock(return_value={"ok": True, "problems": []}),
        ), patch(
            "app.services.alerts.system_alerts.send_system_alert", new=AsyncMock()
        ) as mock_alert:
            await jobs.job_freshness_watchdog()

        mock_alert.assert_not_awaited()
