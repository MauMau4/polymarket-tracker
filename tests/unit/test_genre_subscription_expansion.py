"""Unit tests for the genre subscription-expansion selection logic
(decisions/2026-07-19.md, approved N=30 politics/elections expansion)."""
from unittest.mock import AsyncMock, patch

import pytest

from app.schemas.market import MarketInfo, TokenInfo
from app.tasks.run_genre_subscription_expansion import _collect_genre_markets


def _mkt(market_id, slug, volume):
    return MarketInfo(
        market_id=market_id,
        slug=slug,
        volume=volume,
        tokens=[TokenInfo(token_id=f"tok-{market_id}-yes", outcome="Yes")],
    )


class TestCollectGenreMarkets:
    @pytest.mark.asyncio
    async def test_sorted_descending_by_volume(self):
        markets = [_mkt("1", "a", 100.0), _mkt("2", "b", 500.0), _mkt("3", "c", 50.0)]
        with patch(
            "app.tasks.run_genre_subscription_expansion.fetch_sports_markets_by_tag",
            new=AsyncMock(return_value=markets),
        ):
            ranked = await _collect_genre_markets(["politics"])
        assert [m.market_id for m in ranked] == ["2", "1", "3"]

    @pytest.mark.asyncio
    async def test_micro_lifecycle_excluded(self):
        markets = [_mkt("1", "btc-updown-5m-123", 1000.0), _mkt("2", "normal-market", 10.0)]
        with patch(
            "app.tasks.run_genre_subscription_expansion.fetch_sports_markets_by_tag",
            new=AsyncMock(return_value=markets),
        ):
            ranked = await _collect_genre_markets(["politics"])
        assert [m.market_id for m in ranked] == ["2"]

    @pytest.mark.asyncio
    async def test_dedup_across_overlapping_tags(self):
        markets = [_mkt("1", "a", 100.0)]
        with patch(
            "app.tasks.run_genre_subscription_expansion.fetch_sports_markets_by_tag",
            new=AsyncMock(return_value=markets),
        ):
            ranked = await _collect_genre_markets(["politics", "elections"])
        # Both genres return the same market_id "1" in this mock; must not duplicate.
        assert len(ranked) == 1
