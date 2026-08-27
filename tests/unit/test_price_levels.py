"""Unit tests for generic price-level crossing alerts (decisions/2026-07-19.md
item 3, C-NFL prerequisite)."""
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from unittest.mock import MagicMock

import pytest

from app.services.alerts.price_levels import check_crossing, format_price_level_message


def _make_alert(
    direction="above",
    level="0.60",
    last_price=None,
    last_fired_at=None,
    cooldown_seconds=300,
    active=True,
    label="Test Alert",
):
    a = MagicMock()
    a.direction = direction
    a.level = Decimal(level)
    a.last_price = Decimal(last_price) if last_price is not None else None
    a.last_fired_at = last_fired_at
    a.cooldown_seconds = cooldown_seconds
    a.active = active
    a.label = label
    a.market_id = "0xMarket"
    a.asset_id = "0xAsset"
    return a


NOW = datetime(2026, 7, 19, 12, 0, 0, tzinfo=timezone.utc)


class TestCheckCrossingAbove:
    def test_fresh_crossing_fires(self):
        alert = _make_alert(direction="above", level="0.60", last_price="0.55")
        assert check_crossing(alert, Decimal("0.61"), NOW) is True

    def test_exactly_at_level_fires(self):
        alert = _make_alert(direction="above", level="0.60", last_price="0.55")
        assert check_crossing(alert, Decimal("0.60"), NOW) is True

    def test_still_below_does_not_fire(self):
        alert = _make_alert(direction="above", level="0.60", last_price="0.55")
        assert check_crossing(alert, Decimal("0.58"), NOW) is False

    def test_already_above_does_not_refire(self):
        """Sustained condition — prev was already above level, not a fresh crossing."""
        alert = _make_alert(direction="above", level="0.60", last_price="0.65")
        assert check_crossing(alert, Decimal("0.70"), NOW) is False

    def test_first_observation_never_fires(self):
        """No prior state — just seeds it, even if already past the level."""
        alert = _make_alert(direction="above", level="0.60", last_price=None)
        assert check_crossing(alert, Decimal("0.70"), NOW) is False


class TestCheckCrossingBelow:
    def test_fresh_crossing_fires(self):
        alert = _make_alert(direction="below", level="0.40", last_price="0.45")
        assert check_crossing(alert, Decimal("0.39"), NOW) is True

    def test_still_above_does_not_fire(self):
        alert = _make_alert(direction="below", level="0.40", last_price="0.45")
        assert check_crossing(alert, Decimal("0.42"), NOW) is False


class TestCooldown:
    def test_within_cooldown_suppressed(self):
        alert = _make_alert(
            direction="above",
            level="0.60",
            last_price="0.55",
            last_fired_at=NOW - timedelta(seconds=100),
            cooldown_seconds=300,
        )
        assert check_crossing(alert, Decimal("0.61"), NOW) is False

    def test_after_cooldown_fires_again(self):
        alert = _make_alert(
            direction="above",
            level="0.60",
            last_price="0.55",
            last_fired_at=NOW - timedelta(seconds=301),
            cooldown_seconds=300,
        )
        assert check_crossing(alert, Decimal("0.61"), NOW) is True


class TestInactive:
    def test_inactive_never_fires(self):
        alert = _make_alert(direction="above", level="0.60", last_price="0.55", active=False)
        assert check_crossing(alert, Decimal("0.61"), NOW) is False


class TestFormatMessage:
    def test_above_message_contains_direction_and_prices(self):
        alert = _make_alert(direction="above", level="0.60", label="C-NFL TP Chiefs")
        text = format_price_level_message(alert, Decimal("0.61"))
        assert "C-NFL TP Chiefs" in text
        assert "above" in text
        assert "0.600" in text
        assert "0.610" in text

    def test_invalid_direction_raises(self):
        alert = _make_alert(direction="sideways", level="0.60")
        with pytest.raises(ValueError):
            check_crossing(alert, Decimal("0.61"), NOW)
