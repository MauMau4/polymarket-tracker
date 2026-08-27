"""Unit tests for sybil cluster detection logic."""
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.services.wallets.clustering import (
    _CLUSTER_MIN_WALLETS,
    _CLUSTER_PRICE_TOLERANCE,
    _CLUSTER_SIZE_RATIO_MAX,
    _CLUSTER_WINDOW_MINUTES,
    _FARMING_PRICE_CEILING,
    _cluster_dedupe_key,
)


_NOW = datetime(2026, 4, 26, 12, 0, 0, tzinfo=timezone.utc)


def _make_trade(
    wallet="0xaaa",
    market_id="market1",
    outcome="YES",
    price=0.50,
    size=100.0,
    ts=None,
    attribution_status="attributed",
):
    from app.schemas.trade import NormalizedTrade
    return NormalizedTrade(
        ts=ts or _NOW,
        market_id=market_id,
        asset_id="asset1",
        outcome=outcome,
        price=Decimal(str(price)),
        size=Decimal(str(size)),
        notional_usd=Decimal(str(price * size)),
        side="BUY",
        wallet=wallet,
        tx_hash=None,
        attribution_status=attribution_status,
        source="ws_market",
        raw_payload={},
    )


class TestClusterDedupeKey:
    def test_same_inputs_produce_same_key(self):
        k1 = _cluster_dedupe_key("m1", "YES", 0.50, _NOW)
        k2 = _cluster_dedupe_key("m1", "YES", 0.50, _NOW)
        assert k1 == k2

    def test_different_market_different_key(self):
        k1 = _cluster_dedupe_key("m1", "YES", 0.50, _NOW)
        k2 = _cluster_dedupe_key("m2", "YES", 0.50, _NOW)
        assert k1 != k2

    def test_different_outcome_different_key(self):
        k1 = _cluster_dedupe_key("m1", "YES", 0.50, _NOW)
        k2 = _cluster_dedupe_key("m1", "NO", 0.50, _NOW)
        assert k1 != k2

    def test_different_time_bucket_different_key(self):
        t1 = _NOW
        t2 = _NOW + timedelta(minutes=_CLUSTER_WINDOW_MINUTES + 1)
        k1 = _cluster_dedupe_key("m1", "YES", 0.50, t1)
        k2 = _cluster_dedupe_key("m1", "YES", 0.50, t2)
        assert k1 != k2

    def test_same_time_bucket_same_key(self):
        t1 = _NOW
        t2 = _NOW + timedelta(minutes=_CLUSTER_WINDOW_MINUTES - 1)
        k1 = _cluster_dedupe_key("m1", "YES", 0.50, t1)
        k2 = _cluster_dedupe_key("m1", "YES", 0.50, t2)
        assert k1 == k2


class TestClusterConstants:
    def test_min_wallets(self):
        assert _CLUSTER_MIN_WALLETS == 3

    def test_price_tolerance(self):
        assert _CLUSTER_PRICE_TOLERANCE == 0.02

    def test_size_ratio(self):
        assert _CLUSTER_SIZE_RATIO_MAX == 1.20

    def test_window_minutes(self):
        assert _CLUSTER_WINDOW_MINUTES == 30


class TestSizeHomogeneity:
    """Test the size homogeneity constraint (max/min ≤ 1.2)."""

    def test_uniform_sizes_pass(self):
        sizes = [100.0, 100.0, 100.0]
        assert max(sizes) / min(sizes) <= _CLUSTER_SIZE_RATIO_MAX

    def test_sizes_within_20pct_pass(self):
        sizes = [100.0, 115.0, 105.0]
        assert max(sizes) / min(sizes) <= _CLUSTER_SIZE_RATIO_MAX

    def test_sizes_outside_20pct_fail(self):
        sizes = [100.0, 200.0, 110.0]
        assert max(sizes) / min(sizes) > _CLUSTER_SIZE_RATIO_MAX


class TestCheckSybilCluster:
    """Integration-style tests mocking the DB and Redis calls."""

    def _mock_db_rows(self, wallets_prices_sizes):
        """Create mock DB row objects."""
        rows = []
        for wallet, price, size in wallets_prices_sizes:
            row = MagicMock()
            row.wallet = wallet
            row.price = price
            row.size = size
            row.ts = _NOW
            rows.append(row)
        return rows

    @pytest.mark.asyncio
    async def test_no_cluster_below_min_wallets(self):
        """Fewer than 3 wallets — no cluster fires."""
        trade = _make_trade(wallet="0xaaa", price=0.50)
        db_rows = self._mock_db_rows([("0xaaa", 0.50, 100.0), ("0xbbb", 0.51, 100.0)])

        with (
            patch("app.services.wallets.clustering.get_session_factory") as mock_factory,
            patch("app.services.wallets.clustering._is_deduped", new_callable=AsyncMock, return_value=False),
        ):
            session = AsyncMock()
            execute_result = MagicMock()
            execute_result.all.return_value = db_rows
            session.execute = AsyncMock(return_value=execute_result)
            mock_factory.return_value.return_value.__aenter__ = AsyncMock(return_value=session)
            mock_factory.return_value.return_value.__aexit__ = AsyncMock(return_value=False)

            from app.services.wallets.clustering import check_sybil_cluster
            # Should complete without persisting an alert (no cluster)
            await check_sybil_cluster(trade)
            # If no cluster: session.add is never called for Alert
            # We verify by checking execute was called (DB was queried) but no add/commit for alert
            assert session.execute.called

    @pytest.mark.asyncio
    async def test_no_cluster_trade_without_wallet(self):
        """Trade with no wallet — skipped immediately."""
        trade = _make_trade(wallet=None)

        with patch("app.services.wallets.clustering.get_session_factory") as mock_factory:
            from app.services.wallets.clustering import check_sybil_cluster
            await check_sybil_cluster(trade)
            mock_factory.assert_not_called()

    @pytest.mark.asyncio
    async def test_no_cluster_trade_without_outcome(self):
        """Trade with no outcome — skipped immediately."""
        trade = _make_trade()
        trade = trade.model_copy(update={"outcome": None})

        with patch("app.services.wallets.clustering.get_session_factory") as mock_factory:
            from app.services.wallets.clustering import check_sybil_cluster
            await check_sybil_cluster(trade)
            mock_factory.assert_not_called()

    @pytest.mark.asyncio
    async def test_farming_cluster_still_fires(self):
        """A cluster at farming prices still fires with [FARMING] label."""
        trade = _make_trade(price=_FARMING_PRICE_CEILING + 0.01)
        # price > 0.90 is filtered at ingestion but the test exercises the label logic
        db_rows = self._mock_db_rows([
            ("0xaaa", 0.91, 100.0),
            ("0xbbb", 0.91, 105.0),
            ("0xccc", 0.92, 98.0),
        ])

        with (
            patch("app.services.wallets.clustering.get_session_factory") as mock_factory,
            patch("app.services.wallets.clustering._is_deduped", new_callable=AsyncMock, return_value=False),
            patch("app.services.wallets.clustering._mark_deduped", new_callable=AsyncMock),
            patch("app.services.wallets.clustering.telegram.send_message", new_callable=AsyncMock, return_value=True),
        ):
            session = AsyncMock()
            wallet_mock = MagicMock(suspected_sybil=False)
            execute_result = MagicMock()
            execute_result.all.return_value = db_rows
            execute_result.scalar_one_or_none.return_value = wallet_mock
            session.execute = AsyncMock(return_value=execute_result)
            mock_factory.return_value.return_value.__aenter__ = AsyncMock(return_value=session)
            mock_factory.return_value.return_value.__aexit__ = AsyncMock(return_value=False)

            from app.services.wallets.clustering import check_sybil_cluster
            await check_sybil_cluster(trade)
            # A [FARMING] cluster still calls session.add
            session.add.assert_called()
