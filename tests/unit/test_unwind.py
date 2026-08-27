"""Unit tests for position unwind detection logic."""
from datetime import datetime, timezone
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.services.wallets.unwind import (
    _UNWIND_PRICE_MOVE,
    _UNWIND_SIZE_THRESHOLD,
    _opposite_side,
)


_NOW = datetime(2026, 4, 26, 12, 0, 0, tzinfo=timezone.utc)


def _make_trade(
    wallet="0xaaa",
    market_id="market1",
    outcome="YES",
    price=0.70,
    size=50.0,
    side="SELL",
    ts=None,
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
        side=side,
        wallet=wallet,
        tx_hash=None,
        attribution_status="attributed",
        source="ws_market",
        raw_payload={},
    )


def _mock_prior_position(total_size: float, avg_price: float, count: int = 3):
    """Create a mock SQLAlchemy row for the prior position aggregate."""
    row = MagicMock()
    row.total_size = total_size
    row.avg_price = avg_price
    row.trade_count = count
    return row


class TestOppositeSide:
    def test_buy_returns_sell(self):
        assert _opposite_side("BUY") == "SELL"

    def test_sell_returns_buy(self):
        assert _opposite_side("SELL") == "BUY"

    def test_case_insensitive(self):
        assert _opposite_side("buy") == "SELL"

    def test_none_returns_none(self):
        assert _opposite_side(None) is None


class TestUnwindConstants:
    def test_size_threshold(self):
        assert _UNWIND_SIZE_THRESHOLD == 0.50

    def test_price_move_threshold(self):
        assert _UNWIND_PRICE_MOVE == 0.05


class TestCheckUnwind:
    @pytest.mark.asyncio
    async def test_no_unwind_for_buy_trade(self):
        """BUY trades are never unwinds — only SELL triggers detection."""
        trade = _make_trade(side="BUY")
        with patch("app.services.wallets.unwind.get_session_factory") as mock_factory:
            from app.services.wallets.unwind import check_unwind
            await check_unwind(trade)
            mock_factory.assert_not_called()

    @pytest.mark.asyncio
    async def test_no_unwind_missing_wallet(self):
        trade = _make_trade(wallet=None)
        with patch("app.services.wallets.unwind.get_session_factory") as mock_factory:
            from app.services.wallets.unwind import check_unwind
            await check_unwind(trade)
            mock_factory.assert_not_called()

    @pytest.mark.asyncio
    async def test_no_unwind_if_no_prior_position(self):
        """No prior buys → no unwind."""
        trade = _make_trade(side="SELL", price=0.70, size=50.0)
        prior = _mock_prior_position(total_size=0.0, avg_price=0.50, count=0)

        with (
            patch("app.services.wallets.unwind.get_session_factory") as mock_factory,
        ):
            session = AsyncMock()
            execute_result = MagicMock()
            execute_result.one.return_value = prior
            session.execute = AsyncMock(return_value=execute_result)
            mock_factory.return_value.return_value.__aenter__ = AsyncMock(return_value=session)
            mock_factory.return_value.return_value.__aexit__ = AsyncMock(return_value=False)

            from app.services.wallets.unwind import check_unwind
            await check_unwind(trade)
            # No alert persisted (session.add never called)
            session.add.assert_not_called()

    @pytest.mark.asyncio
    async def test_no_unwind_if_size_below_threshold(self):
        """Sell size < 50% of prior position → not an unwind."""
        trade = _make_trade(side="SELL", price=0.70, size=20.0)  # 20/100 = 20%
        prior = _mock_prior_position(total_size=100.0, avg_price=0.60, count=3)

        with (
            patch("app.services.wallets.unwind.get_session_factory") as mock_factory,
        ):
            session = AsyncMock()
            execute_result = MagicMock()
            execute_result.one.return_value = prior
            session.execute = AsyncMock(return_value=execute_result)
            mock_factory.return_value.return_value.__aenter__ = AsyncMock(return_value=session)
            mock_factory.return_value.return_value.__aexit__ = AsyncMock(return_value=False)

            from app.services.wallets.unwind import check_unwind
            await check_unwind(trade)
            session.add.assert_not_called()

    @pytest.mark.asyncio
    async def test_no_unwind_if_price_move_too_small(self):
        """Price moved < 0.05 from entry → not an unwind."""
        trade = _make_trade(side="SELL", price=0.62, size=60.0)  # move = 0.62-0.60 = 0.02
        prior = _mock_prior_position(total_size=100.0, avg_price=0.60, count=3)

        with (
            patch("app.services.wallets.unwind.get_session_factory") as mock_factory,
        ):
            session = AsyncMock()
            execute_result = MagicMock()
            execute_result.one.return_value = prior
            session.execute = AsyncMock(return_value=execute_result)
            mock_factory.return_value.return_value.__aenter__ = AsyncMock(return_value=session)
            mock_factory.return_value.return_value.__aexit__ = AsyncMock(return_value=False)

            from app.services.wallets.unwind import check_unwind
            await check_unwind(trade)
            session.add.assert_not_called()

    @pytest.mark.asyncio
    async def test_unwind_fires_on_profit(self):
        """Valid unwind: sell >= 50% of prior, price moved >= 0.05, profit direction."""
        trade = _make_trade(side="SELL", price=0.75, size=60.0)  # 60% of prior; move = 0.15
        prior = _mock_prior_position(total_size=100.0, avg_price=0.60, count=3)

        with (
            patch("app.services.wallets.unwind.get_session_factory") as mock_factory,
            patch("app.services.wallets.unwind._is_deduped", new_callable=AsyncMock, return_value=False),
            patch("app.services.wallets.unwind._mark_deduped", new_callable=AsyncMock),
            patch("app.services.wallets.unwind.telegram.send_message", new_callable=AsyncMock, return_value=True),
        ):
            session = AsyncMock()
            trade_obj = MagicMock(trade_type=None)
            execute_result = MagicMock()
            execute_result.one.return_value = prior
            execute_result.scalar_one_or_none.return_value = trade_obj
            session.execute = AsyncMock(return_value=execute_result)
            mock_factory.return_value.return_value.__aenter__ = AsyncMock(return_value=session)
            mock_factory.return_value.return_value.__aexit__ = AsyncMock(return_value=False)

            from app.services.wallets.unwind import check_unwind
            await check_unwind(trade)
            session.add.assert_called()  # Alert was persisted

    @pytest.mark.asyncio
    async def test_unwind_deduped(self):
        """Same unwind within dedupe window doesn't fire twice."""
        trade = _make_trade(side="SELL", price=0.75, size=60.0)
        prior = _mock_prior_position(total_size=100.0, avg_price=0.60, count=3)

        with (
            patch("app.services.wallets.unwind.get_session_factory") as mock_factory,
            patch("app.services.wallets.unwind._is_deduped", new_callable=AsyncMock, return_value=True),
        ):
            session = AsyncMock()
            execute_result = MagicMock()
            execute_result.one.return_value = prior
            session.execute = AsyncMock(return_value=execute_result)
            mock_factory.return_value.return_value.__aenter__ = AsyncMock(return_value=session)
            mock_factory.return_value.return_value.__aexit__ = AsyncMock(return_value=False)

            from app.services.wallets.unwind import check_unwind
            await check_unwind(trade)
            session.add.assert_not_called()
