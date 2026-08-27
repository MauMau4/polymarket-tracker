"""Unit tests for the discovery-horizon watchlist bypass (decisions/2026-07-19.md
item 2): watchlisted championship/conference books must be ingested even
though their shared event end_date sits far beyond the ordinary 30-day
discovery horizon."""
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch

from app.schemas.market import MarketInfo
from app.services.discovery.refresh import _is_watchlisted, _within_discovery_horizon


def _make_market_info(
    event_title: str | None = None,
    end_date=None,
    resolved: bool = False,
) -> MarketInfo:
    return MarketInfo(
        market_id="123",
        condition_id="0xcond",
        slug="some-team-market",
        question="Will Team X win?",
        event_title=event_title,
        category="Sports",
        active=True,
        closed=False,
        resolved=resolved,
        resolution=None,
        end_date=end_date,
        volume=1000.0,
        tokens=[],
    )


def _mock_config(watchlist_titles):
    cfg = MagicMock()
    cfg.booklog.watchlist_event_titles = watchlist_titles
    return cfg


class TestIsWatchlisted:
    def test_matching_title_is_watchlisted(self):
        info = _make_market_info(event_title="NFL Champion 2027")
        with patch("pathfinder.config.get_config", return_value=_mock_config(["NFL Champion 2027"])):
            assert _is_watchlisted(info) is True

    def test_non_matching_title_not_watchlisted(self):
        info = _make_market_info(event_title="Some Random Event")
        with patch("pathfinder.config.get_config", return_value=_mock_config(["NFL Champion 2027"])):
            assert _is_watchlisted(info) is False

    def test_no_event_title_not_watchlisted(self):
        info = _make_market_info(event_title=None)
        with patch("pathfinder.config.get_config", return_value=_mock_config(["NFL Champion 2027"])):
            assert _is_watchlisted(info) is False

    def test_config_load_failure_fails_closed(self):
        """If pathfinder config can't load, never silently admit a market —
        fail closed (not watchlisted), not open."""
        info = _make_market_info(event_title="NFL Champion 2027")
        with patch("pathfinder.config.get_config", side_effect=Exception("boom")):
            assert _is_watchlisted(info) is False


class TestWithinDiscoveryHorizon:
    def test_far_future_end_date_normally_excluded(self):
        info = _make_market_info(
            event_title="Some Random Event",
            end_date=datetime.now(tz=timezone.utc) + timedelta(days=200),
        )
        with patch("pathfinder.config.get_config", return_value=_mock_config([])):
            assert _within_discovery_horizon(info) is False

    def test_far_future_end_date_admitted_if_watchlisted(self):
        info = _make_market_info(
            event_title="NFL Champion 2027",
            end_date=datetime.now(tz=timezone.utc) + timedelta(days=200),
        )
        with patch("pathfinder.config.get_config", return_value=_mock_config(["NFL Champion 2027"])):
            assert _within_discovery_horizon(info) is True

    def test_within_30_days_admitted_regardless(self):
        info = _make_market_info(
            event_title="Some Random Event",
            end_date=datetime.now(tz=timezone.utc) + timedelta(days=5),
        )
        with patch("pathfinder.config.get_config", return_value=_mock_config([])):
            assert _within_discovery_horizon(info) is True

    def test_resolved_always_admitted(self):
        info = _make_market_info(
            event_title="Some Random Event",
            end_date=datetime.now(tz=timezone.utc) + timedelta(days=200),
            resolved=True,
        )
        with patch("pathfinder.config.get_config", return_value=_mock_config([])):
            assert _within_discovery_horizon(info) is True

    def test_no_end_date_always_admitted(self):
        info = _make_market_info(event_title="Some Random Event", end_date=None)
        with patch("pathfinder.config.get_config", return_value=_mock_config([])):
            assert _within_discovery_horizon(info) is True
