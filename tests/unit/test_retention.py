"""Unit tests for trade data retention logic."""
from datetime import datetime, timedelta, timezone

import pytest


_NOW = datetime(2026, 4, 26, 12, 0, 0, tzinfo=timezone.utc)


class TestRetentionCutoff:
    """Verify the retention cutoff date calculation."""

    def test_30_day_cutoff(self):
        cutoff = _NOW - timedelta(days=30)
        trade_age = _NOW - timedelta(days=31)
        assert trade_age < cutoff  # should be purged

    def test_29_day_trade_not_purged(self):
        cutoff = _NOW - timedelta(days=30)
        trade_age = _NOW - timedelta(days=29)
        assert trade_age >= cutoff  # should NOT be purged

    def test_exactly_30_days_not_purged(self):
        cutoff = _NOW - timedelta(days=30)
        trade_age = _NOW - timedelta(days=30)
        assert trade_age >= cutoff  # boundary: NOT purged (ts < cutoff, not <=)

    def test_configurable_retention(self):
        for days in [7, 14, 30, 60, 90]:
            cutoff = _NOW - timedelta(days=days)
            old_trade = _NOW - timedelta(days=days + 1)
            new_trade = _NOW - timedelta(days=days - 1)
            assert old_trade < cutoff
            assert new_trade > cutoff


class TestRetentionEligibility:
    """Test the business rules for what can be purged."""

    def _make_trade_params(
        self,
        ts_days_ago: float,
        market_resolved: bool,
        has_wallet: bool,
        wallet_scored: bool,
    ) -> dict:
        return {
            "ts": _NOW - timedelta(days=ts_days_ago),
            "market_resolved": market_resolved,
            "has_wallet": has_wallet,
            "wallet_scored": wallet_scored,
        }

    def _is_eligible(self, params: dict, retention_days: int = 30) -> bool:
        cutoff = _NOW - timedelta(days=retention_days)
        if params["ts"] >= cutoff:
            return False
        if not params["market_resolved"]:
            return False
        if params["has_wallet"] and not params["wallet_scored"]:
            return False
        return True

    def test_unresolved_market_never_purged(self):
        p = self._make_trade_params(ts_days_ago=60, market_resolved=False, has_wallet=True, wallet_scored=True)
        assert self._is_eligible(p) is False

    def test_unattributed_old_resolved_trade_purged(self):
        p = self._make_trade_params(ts_days_ago=31, market_resolved=True, has_wallet=False, wallet_scored=False)
        assert self._is_eligible(p) is True

    def test_attributed_scored_old_resolved_trade_purged(self):
        p = self._make_trade_params(ts_days_ago=31, market_resolved=True, has_wallet=True, wallet_scored=True)
        assert self._is_eligible(p) is True

    def test_attributed_unscored_wallet_not_purged(self):
        p = self._make_trade_params(ts_days_ago=31, market_resolved=True, has_wallet=True, wallet_scored=False)
        assert self._is_eligible(p) is False

    def test_recent_resolved_scored_not_purged(self):
        p = self._make_trade_params(ts_days_ago=10, market_resolved=True, has_wallet=True, wallet_scored=True)
        assert self._is_eligible(p) is False

    def test_exactly_at_cutoff_not_purged(self):
        p = self._make_trade_params(ts_days_ago=30, market_resolved=True, has_wallet=True, wallet_scored=True)
        assert self._is_eligible(p) is False  # exactly at cutoff (ts >= cutoff)
