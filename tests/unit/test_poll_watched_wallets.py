"""
Tests for app/tasks/poll_watched_wallets.py — pagination against the Data
API's real (newest-first, offset-only) behavior, the two-part dedup guard
against same-run pagination drift, batch-commit rollback isolation, and
oldest-first ordering surviving batching.

See decisions/2026-08-06.md for the live-API pagination findings and the
item 4 (multi-fill dedup key) negative result these changes rest on.

Pure-function tests (_filter_and_sort_window, _fetch_wallet_trades against a
respx-mocked transport) need no database. Anything that exercises poll_wallet
end-to-end needs a real Postgres session, per this repo's convention for
DB-touching tests (tests/conftest.py's db_session / session_factory
fixtures) — poll_wallet opens its own session via get_session_factory(), so
those tests patch that factory to point at the test database rather than
mocking the session away.
"""
import datetime as dt

import httpx
import pytest
import respx
from sqlalchemy import select

from app.db.models.market import Market
from app.db.models.token import Token
from app.db.models.trade import Trade
from app.db.models.wallet import Wallet
from app.tasks import poll_watched_wallets as poll_mod
from app.tasks.poll_watched_wallets import (
    FetchResult,
    _estimate_trades_per_day,
    _fetch_wallet_trades,
    _filter_and_sort_window,
    _load_captured_keys,
    poll_wallet,
    run,
)

_TRADES_URL = "https://data-api.polymarket.com/trades"
_WALLET = "0xpollwallet0000000000000000000000000000"


def _mk_trade(
    ts: int,
    tx_hash: str,
    asset: str = "asset-1",
    side: str = "BUY",
    price: float = 0.5,
    size: float = 10.0,
    outcome: str = "Yes",
    condition_id: str = "cond-1",
    wallet: str = _WALLET,
) -> dict:
    return {
        "proxyWallet": wallet,
        "side": side,
        "asset": asset,
        "conditionId": condition_id,
        "size": size,
        "price": price,
        "timestamp": ts,
        "outcome": outcome,
        "transactionHash": tx_hash,
    }


async def _seed_market(db_session, market_id: str, asset_id: str, condition_id: str, outcome: str = "Yes"):
    db_session.add(Market(
        market_id=market_id, condition_id=condition_id, slug=market_id, question="q",
        active=True, closed=False, resolved=False,
    ))
    db_session.add(Token(asset_id=asset_id, market_id=market_id, outcome=outcome, token_id=asset_id))
    await db_session.flush()


# ---------------------------------------------------------------------------
# _fetch_wallet_trades — pagination against a mocked Data API
# ---------------------------------------------------------------------------

class TestPagination:
    @respx.mock
    async def test_assembles_multiple_pages_and_stops_on_short_page(self):
        now = int(dt.datetime.now(tz=dt.timezone.utc).timestamp())
        page0 = [_mk_trade(now - i, f"tx-a-{i}") for i in range(500)]
        page1 = [_mk_trade(now - 500 - i, f"tx-b-{i}") for i in range(200)]  # short -> exhausted
        calls: list[int] = []

        def _responder(request: httpx.Request) -> httpx.Response:
            offset = int(request.url.params.get("offset", "0"))
            calls.append(offset)
            if offset == 0:
                return httpx.Response(200, json=page0)
            if offset == 500:
                return httpx.Response(200, json=page1)
            raise AssertionError(f"unexpected offset {offset}")

        respx.get(_TRADES_URL).mock(side_effect=_responder)

        cutoff = dt.datetime.fromtimestamp(now - 100_000, tz=dt.timezone.utc)
        result = await _fetch_wallet_trades(_WALLET, cutoff)

        assert calls == [0, 500]
        assert len(result.trades) == 700
        assert result.truncated is False

    @respx.mock
    async def test_stops_without_extra_page_when_full_page_already_predates_cutoff(self):
        now = int(dt.datetime.now(tz=dt.timezone.utc).timestamp())
        # Full page (== limit) spanning ~8.3h; cutoff sits well inside it.
        page0 = [_mk_trade(now - i * 60, f"tx-{i}") for i in range(500)]
        calls: list[int] = []

        def _responder(request: httpx.Request) -> httpx.Response:
            calls.append(int(request.url.params.get("offset", "0")))
            return httpx.Response(200, json=page0)

        respx.get(_TRADES_URL).mock(side_effect=_responder)

        cutoff = dt.datetime.fromtimestamp(now - 3600, tz=dt.timezone.utc)
        result = await _fetch_wallet_trades(_WALLET, cutoff)

        assert calls == [0]  # never asked for offset=500
        assert len(result.trades) == 500
        assert result.truncated is False

    @respx.mock
    async def test_continues_past_full_page_when_cutoff_not_yet_reached(self):
        """The risky boundary case: page0 is full (len == limit) so a naive
        stop-on-short-page rule would keep going regardless, but a naive
        stop-on-cutoff rule checked against the wrong row could stop one
        page too early. Page0's oldest row (now-499) is still >= cutoff
        (now-700), so pagination must continue to page1 to pick up the
        remaining in-window rows (now-500 .. now-700)."""
        now = int(dt.datetime.now(tz=dt.timezone.utc).timestamp())
        page0 = [_mk_trade(now - i, f"tx-a-{i}") for i in range(500)]  # now .. now-499
        page1 = [_mk_trade(now - 500 - i, f"tx-b-{i}") for i in range(500)]  # now-500 .. now-999
        calls: list[int] = []

        def _responder(request: httpx.Request) -> httpx.Response:
            offset = int(request.url.params.get("offset", "0"))
            calls.append(offset)
            if offset == 0:
                return httpx.Response(200, json=page0)
            if offset == 500:
                return httpx.Response(200, json=page1)
            raise AssertionError("should not have requested a third page")

        respx.get(_TRADES_URL).mock(side_effect=_responder)

        cutoff = dt.datetime.fromtimestamp(now - 700, tz=dt.timezone.utc)
        raw = await _fetch_wallet_trades(_WALLET, cutoff)
        recent = _filter_and_sort_window(raw.trades, cutoff)

        assert calls == [0, 500]
        assert len(recent) == 701  # now down to now-700 inclusive, none dropped

    @respx.mock
    async def test_empty_page_terminates_without_error(self):
        respx.get(_TRADES_URL).mock(return_value=httpx.Response(200, json=[]))
        cutoff = dt.datetime.now(tz=dt.timezone.utc) - dt.timedelta(days=1)
        result = await _fetch_wallet_trades(_WALLET, cutoff)
        assert result.trades == []
        assert result.truncated is False

    @respx.mock
    async def test_http_error_stops_pagination_and_reports_truncated(self):
        now = int(dt.datetime.now(tz=dt.timezone.utc).timestamp())
        page0 = [_mk_trade(now - i, f"tx-a-{i}") for i in range(500)]
        calls: list[int] = []

        def _responder(request: httpx.Request) -> httpx.Response:
            offset = int(request.url.params.get("offset", "0"))
            calls.append(offset)
            if offset == 0:
                return httpx.Response(200, json=page0)
            return httpx.Response(500, json={"error": "boom"})

        respx.get(_TRADES_URL).mock(side_effect=_responder)

        cutoff = dt.datetime.fromtimestamp(now - 100_000, tz=dt.timezone.utc)
        result = await _fetch_wallet_trades(_WALLET, cutoff)

        assert calls == [0, 500]
        assert len(result.trades) == 500  # page0 kept, page1's error didn't wipe it out
        assert result.truncated is True
        assert result.truncation_reason == "http_error"


# ---------------------------------------------------------------------------
# 429 retry with backoff (item 16) — distinct from a genuine error, which
# still breaks immediately as tested above.
# ---------------------------------------------------------------------------

class TestRateLimitRetry:
    @respx.mock
    async def test_429_retries_then_succeeds(self, monkeypatch):
        sleeps: list[float] = []

        async def _fake_sleep(seconds):
            sleeps.append(seconds)

        monkeypatch.setattr(poll_mod.asyncio, "sleep", _fake_sleep)

        now = int(dt.datetime.now(tz=dt.timezone.utc).timestamp())
        page0 = [_mk_trade(now - i, f"tx-{i}") for i in range(3)]  # short -> exhausted
        calls = {"n": 0}

        def _responder(request: httpx.Request) -> httpx.Response:
            calls["n"] += 1
            if calls["n"] == 1:
                return httpx.Response(429)
            return httpx.Response(200, json=page0)

        respx.get(_TRADES_URL).mock(side_effect=_responder)

        cutoff = dt.datetime.fromtimestamp(now - 100_000, tz=dt.timezone.utc)
        result = await _fetch_wallet_trades(_WALLET, cutoff)

        assert calls["n"] == 2  # first 429, retried and succeeded — no break
        assert result.truncated is False
        assert len(result.trades) == 3
        assert sleeps == [2.0]  # base delay, no Retry-After header on the mock

    @respx.mock
    async def test_429_exhausts_retries_and_reports_truncated_rate_limited(self, monkeypatch):
        sleeps: list[float] = []

        async def _fake_sleep(seconds):
            sleeps.append(seconds)

        monkeypatch.setattr(poll_mod.asyncio, "sleep", _fake_sleep)
        respx.get(_TRADES_URL).mock(return_value=httpx.Response(429))

        cutoff = dt.datetime.now(tz=dt.timezone.utc) - dt.timedelta(days=1)
        result = await _fetch_wallet_trades(_WALLET, cutoff)

        assert result.trades == []
        assert result.truncated is True
        assert result.truncation_reason == "rate_limited"
        assert len(sleeps) == poll_mod._RATE_LIMIT_MAX_RETRIES

    @respx.mock
    async def test_429_honors_retry_after_header_when_present(self, monkeypatch):
        sleeps: list[float] = []

        async def _fake_sleep(seconds):
            sleeps.append(seconds)

        monkeypatch.setattr(poll_mod.asyncio, "sleep", _fake_sleep)
        calls = {"n": 0}

        def _responder(request: httpx.Request) -> httpx.Response:
            calls["n"] += 1
            if calls["n"] == 1:
                return httpx.Response(429, headers={"Retry-After": "7"})
            return httpx.Response(200, json=[])

        respx.get(_TRADES_URL).mock(side_effect=_responder)

        cutoff = dt.datetime.now(tz=dt.timezone.utc) - dt.timedelta(days=1)
        result = await _fetch_wallet_trades(_WALLET, cutoff)

        assert sleeps == [7.0]  # header value used, not the 2.0s computed default
        assert result.truncated is False

    @respx.mock
    async def test_non_429_error_still_breaks_immediately_without_retry(self, monkeypatch):
        """Genuine errors are not retried — same behavior as before this
        change, just now reported as a truncation rather than silent."""
        sleeps: list[float] = []

        async def _fake_sleep(seconds):
            sleeps.append(seconds)

        monkeypatch.setattr(poll_mod.asyncio, "sleep", _fake_sleep)
        respx.get(_TRADES_URL).mock(return_value=httpx.Response(500))

        cutoff = dt.datetime.now(tz=dt.timezone.utc) - dt.timedelta(days=1)
        result = await _fetch_wallet_trades(_WALLET, cutoff)

        assert sleeps == []  # never slept/retried
        assert result.truncated is True
        assert result.truncation_reason == "http_error"


# ---------------------------------------------------------------------------
# _filter_and_sort_window — oldest-first ordering, cutoff trimming
# ---------------------------------------------------------------------------

class TestFilterAndSortWindow:
    def test_sorts_oldest_first_even_from_newest_first_input(self):
        now = 2_000_000_000
        raw = [_mk_trade(now, "tx-newest"), _mk_trade(now - 100, "tx-mid"), _mk_trade(now - 200, "tx-oldest")]
        cutoff = dt.datetime.fromtimestamp(now - 1000, tz=dt.timezone.utc)
        recent = _filter_and_sort_window(raw, cutoff)
        assert [t["transactionHash"] for t in recent] == ["tx-oldest", "tx-mid", "tx-newest"]

    def test_drops_rows_before_cutoff(self):
        now = 2_000_000_000
        raw = [_mk_trade(now, "tx-in"), _mk_trade(now - 1_000_000, "tx-out")]
        cutoff = dt.datetime.fromtimestamp(now - 10, tz=dt.timezone.utc)
        recent = _filter_and_sort_window(raw, cutoff)
        assert [t["transactionHash"] for t in recent] == ["tx-in"]

    def test_ordering_survives_trades_assembled_from_multiple_pages(self):
        """Pages arrive newest-first and are concatenated in that order by
        _fetch_wallet_trades; _filter_and_sort_window must still produce a
        single oldest-first sequence across the whole assembled list, since
        poll_wallet's BUY-before-SELL guarantee depends on this, not on
        pagination happening to preserve order."""
        now = 2_000_000_000
        page0 = [_mk_trade(now - i, f"tx-{i}") for i in range(5)]  # now..now-4, newest-first
        page1 = [_mk_trade(now - 5 - i, f"tx-{5+i}") for i in range(5)]  # now-5..now-9
        raw = page0 + page1
        cutoff = dt.datetime.fromtimestamp(now - 1000, tz=dt.timezone.utc)
        recent = _filter_and_sort_window(raw, cutoff)
        assert [t["transactionHash"] for t in recent] == [f"tx-{i}" for i in range(9, -1, -1)]


# ---------------------------------------------------------------------------
# _load_captured_keys — batched dedup, wallet-scoped not window-scoped
# ---------------------------------------------------------------------------

class TestLoadCapturedKeys:
    async def test_finds_existing_key_regardless_of_trade_age(self, db_session):
        """Deliberately not window-scoped (see the docstring on
        _load_captured_keys): a trade far outside any realistic lookback
        window must still be found, matching the original per-trade check's
        unscoped existence semantics."""
        market_id, asset_id = "test-poll-captured-market", "test-poll-captured-asset"
        await _seed_market(db_session, market_id, asset_id, "cond-captured")
        old_ts = dt.datetime.now(tz=dt.timezone.utc) - dt.timedelta(days=400)
        db_session.add(Trade(
            ts=old_ts, market_id=market_id, asset_id=asset_id, outcome="Yes",
            price=0.5, size=10.0, side="BUY", wallet=_WALLET,
            tx_hash="tx-old", source="clob_poll", external_trade_id="clob_poll-tx-old",
        ))
        await db_session.commit()

        keys = await _load_captured_keys(db_session, _WALLET)
        assert ("tx-old", asset_id) in keys

    async def test_does_not_include_other_wallets_trades(self, db_session):
        market_id, asset_id = "test-poll-otherwallet-market", "test-poll-otherwallet-asset"
        await _seed_market(db_session, market_id, asset_id, "cond-other")
        db_session.add(Trade(
            ts=dt.datetime.now(tz=dt.timezone.utc), market_id=market_id, asset_id=asset_id,
            outcome="Yes", price=0.5, size=10.0, side="BUY", wallet="0xsomeoneelse",
            tx_hash="tx-someone-else", source="clob_poll", external_trade_id="clob_poll-tx-someone-else",
        ))
        await db_session.commit()

        keys = await _load_captured_keys(db_session, _WALLET)
        assert ("tx-someone-else", asset_id) not in keys


# ---------------------------------------------------------------------------
# poll_wallet — end-to-end against real Postgres (session_factory patched in)
# ---------------------------------------------------------------------------

class TestPollWalletDedupAndBatching:
    async def test_duplicate_trade_in_one_fetch_payload_inserts_once(
        self, db_session, session_factory, monkeypatch
    ):
        """The pagination-drift scenario: the same fill appears twice in one
        run's fetched payload. The DB-loaded set alone can't catch this
        (neither copy is committed when it's built) -- the in-run seen-set
        must."""
        market_id, asset_id = "test-poll-dup-market", "test-poll-dup-asset"
        await _seed_market(db_session, market_id, asset_id, "cond-dup")
        await db_session.commit()

        now = dt.datetime.now(tz=dt.timezone.utc)
        raw = _mk_trade(int(now.timestamp()), "tx-dup", asset=asset_id, condition_id="cond-dup")
        dup_payload = [dict(raw), dict(raw)]

        async def _fake_fetch(wallet, cutoff_ts, limit=500):
            return FetchResult(dup_payload)

        monkeypatch.setattr(poll_mod, "_fetch_wallet_trades", _fake_fetch)
        monkeypatch.setattr(poll_mod, "get_session_factory", lambda: session_factory)

        summary = await poll_wallet(_WALLET, "watch", now - dt.timedelta(days=1))

        assert summary["inserted"] == 1
        assert summary["skipped"] == 1
        assert summary["aborted"] is False

        rows = (await db_session.execute(
            select(Trade).where(Trade.tx_hash == "tx-dup", Trade.asset_id == asset_id)
        )).scalars().all()
        assert len(rows) == 1

    async def test_pre_existing_trade_is_skipped_not_reinserted(
        self, db_session, session_factory, monkeypatch
    ):
        market_id, asset_id = "test-poll-preexisting-market", "test-poll-preexisting-asset"
        await _seed_market(db_session, market_id, asset_id, "cond-preexisting")
        now = dt.datetime.now(tz=dt.timezone.utc)
        db_session.add(Trade(
            ts=now, market_id=market_id, asset_id=asset_id, outcome="Yes",
            price=0.5, size=10.0, side="BUY", wallet=_WALLET,
            tx_hash="tx-existing", source="live_ws", external_trade_id="live_ws-tx-existing",
        ))
        await db_session.commit()

        raw = _mk_trade(int(now.timestamp()), "tx-existing", asset=asset_id, condition_id="cond-preexisting")

        async def _fake_fetch(wallet, cutoff_ts, limit=500):
            return FetchResult([raw])

        monkeypatch.setattr(poll_mod, "_fetch_wallet_trades", _fake_fetch)
        monkeypatch.setattr(poll_mod, "get_session_factory", lambda: session_factory)

        summary = await poll_wallet(_WALLET, "watch", now - dt.timedelta(days=1))

        assert summary["inserted"] == 0
        assert summary["skipped"] == 1
        rows = (await db_session.execute(select(Trade).where(Trade.tx_hash == "tx-existing"))).scalars().all()
        assert len(rows) == 1

    async def test_batching_preserves_oldest_first_processing_order(
        self, db_session, session_factory, monkeypatch
    ):
        """A BUY must be committed before its matching SELL is processed,
        even when both land in different commit batches."""
        market_id, asset_id = "test-poll-order-market", "test-poll-order-asset"
        await _seed_market(db_session, market_id, asset_id, "cond-order")
        await db_session.commit()

        monkeypatch.setattr(poll_mod, "_BATCH_SIZE", 1)
        monkeypatch.setattr(poll_mod, "get_session_factory", lambda: session_factory)

        now = dt.datetime.now(tz=dt.timezone.utc)
        base_ts = int(now.timestamp())
        # Fed newest-first (buy is more recent in this raw ordering) --
        # poll_wallet must still process the BUY (older) before the SELL.
        raw_trades = [
            _mk_trade(base_ts, "tx-sell", asset=asset_id, condition_id="cond-order", side="SELL", price=0.7),
            _mk_trade(base_ts - 10, "tx-buy", asset=asset_id, condition_id="cond-order", side="BUY", price=0.5),
        ]

        async def _fake_fetch(wallet, cutoff_ts, limit=500):
            return FetchResult(raw_trades)

        monkeypatch.setattr(poll_mod, "_fetch_wallet_trades", _fake_fetch)

        summary = await poll_wallet(_WALLET, "watch", now - dt.timedelta(days=1))

        assert summary["inserted"] == 2
        assert summary["new_sells"] == 1

        from app.db.models.wallet_closed_position import WalletClosedPosition
        wcp_rows = (await db_session.execute(
            select(WalletClosedPosition).where(WalletClosedPosition.wallet == _WALLET)
        )).scalars().all()
        assert len(wcp_rows) == 1, "SELL must have seen the BUY's cost basis despite separate batches"
        assert wcp_rows[0].entry_price == pytest.approx(0.5)
        assert wcp_rows[0].exit_price == pytest.approx(0.7)


class TestPollWalletBatchFailureIsolation:
    async def test_mid_batch_failure_rolls_back_and_does_not_poison_next_wallet(
        self, db_session, session_factory, monkeypatch
    ):
        """Mirrors the decisions/2026-07-19.md genre_discovery scar: a
        caught exception without a rollback poisoned every later operation
        in the same session. Here, a failure partway through one wallet's
        trades must (a) keep the earlier committed batch, (b) discard the
        uncommitted one, and (c) not affect a completely separate wallet
        polled afterward against the same session_factory."""
        market_id, asset_id = "test-poll-rollback-market", "test-poll-rollback-asset"
        await _seed_market(db_session, market_id, asset_id, "cond-rollback")
        await db_session.commit()

        monkeypatch.setattr(poll_mod, "_BATCH_SIZE", 2)
        monkeypatch.setattr(poll_mod, "get_session_factory", lambda: session_factory)

        now = dt.datetime.now(tz=dt.timezone.utc)
        base_ts = int(now.timestamp())
        # tx-rb-0 is newest, tx-rb-4 is oldest; oldest-first processing order
        # is tx-rb-4, tx-rb-3, tx-rb-2, tx-rb-1, tx-rb-0.
        raw_trades = [
            _mk_trade(base_ts - i, f"tx-rb-{i}", asset=asset_id, condition_id="cond-rollback")
            for i in range(5)
        ]

        async def _fake_fetch(wallet, cutoff_ts, limit=500):
            return FetchResult(raw_trades)

        monkeypatch.setattr(poll_mod, "_fetch_wallet_trades", _fake_fetch)

        real_ingest = poll_mod._ingest_trade

        async def _flaky_ingest(session, raw):
            if raw["transactionHash"] == "tx-rb-2":
                raise RuntimeError("simulated ingestion failure")
            return await real_ingest(session, raw)

        monkeypatch.setattr(poll_mod, "_ingest_trade", _flaky_ingest)

        summary = await poll_wallet(_WALLET, "watch", now - dt.timedelta(days=1))

        assert summary["aborted"] is True
        assert summary["inserted"] == 2  # only the first committed batch (tx-rb-4, tx-rb-3)

        rows = (await db_session.execute(select(Trade).where(Trade.asset_id == asset_id))).scalars().all()
        assert {r.tx_hash for r in rows} == {"tx-rb-4", "tx-rb-3"}

        # Prove isolation: poll an unrelated wallet against the same
        # factory and confirm it succeeds cleanly, unaffected by the
        # rollback above.
        market_id2, asset_id2 = "test-poll-rollback-market-2", "test-poll-rollback-asset-2"
        await _seed_market(db_session, market_id2, asset_id2, "cond-rollback-2")
        await db_session.commit()

        other_wallet = "0xotherwallet00000000000000000000000000"
        other_trade = _mk_trade(
            base_ts, "tx-ok", asset=asset_id2, condition_id="cond-rollback-2", wallet=other_wallet
        )

        async def _fake_fetch2(wallet, cutoff_ts, limit=500):
            return FetchResult([other_trade])

        monkeypatch.setattr(poll_mod, "_fetch_wallet_trades", _fake_fetch2)
        monkeypatch.setattr(poll_mod, "_ingest_trade", real_ingest)

        summary2 = await poll_wallet(other_wallet, "watch", now - dt.timedelta(days=1))
        assert summary2["inserted"] == 1
        assert summary2["aborted"] is False

        other_rows = (await db_session.execute(select(Trade).where(Trade.wallet == other_wallet))).scalars().all()
        assert len(other_rows) == 1


# ---------------------------------------------------------------------------
# _estimate_trades_per_day — one-page density probe for --max-trades-per-day
# ---------------------------------------------------------------------------

class TestEstimateTradesPerDay:
    @respx.mock
    async def test_estimates_rate_from_page_span(self):
        now = int(dt.datetime.now(tz=dt.timezone.utc).timestamp())
        # 500 trades evenly spread over exactly 1 day -> ~500 trades/day.
        page = [_mk_trade(now - i * (86400 // 500), f"tx-{i}") for i in range(500)]
        respx.get(_TRADES_URL).mock(return_value=httpx.Response(200, json=page))

        rate = await _estimate_trades_per_day(_WALLET)

        assert rate == pytest.approx(500.0, rel=0.05)

    @respx.mock
    async def test_returns_none_for_fewer_than_two_trades(self):
        respx.get(_TRADES_URL).mock(return_value=httpx.Response(200, json=[_mk_trade(1000, "tx-only")]))
        assert await _estimate_trades_per_day(_WALLET) is None

    @respx.mock
    async def test_returns_none_for_empty_page(self):
        respx.get(_TRADES_URL).mock(return_value=httpx.Response(200, json=[]))
        assert await _estimate_trades_per_day(_WALLET) is None

    @respx.mock
    async def test_returns_none_when_all_trades_share_one_timestamp(self):
        page = [_mk_trade(1000, f"tx-{i}") for i in range(5)]
        respx.get(_TRADES_URL).mock(return_value=httpx.Response(200, json=page))
        assert await _estimate_trades_per_day(_WALLET) is None

    @respx.mock
    async def test_returns_none_on_http_error(self):
        respx.get(_TRADES_URL).mock(return_value=httpx.Response(500))
        assert await _estimate_trades_per_day(_WALLET) is None


# ---------------------------------------------------------------------------
# run() — --max-trades-per-day / --exclude-wallet operational filters
# ---------------------------------------------------------------------------

async def _seed_wallet(db_session, wallet: str, watch_status: str = "watch"):
    now = dt.datetime.now(tz=dt.timezone.utc)
    db_session.add(Wallet(
        wallet=wallet, first_seen=now, last_seen=now, watch_status=watch_status,
    ))
    await db_session.flush()


class TestRunOperationalFilters:
    async def test_max_trades_per_day_skips_high_rate_wallet_without_polling_it(
        self, db_session, session_factory, monkeypatch
    ):
        fast_wallet = "0xfastwallet000000000000000000000000000"
        slow_wallet = "0xslowwallet000000000000000000000000000"
        await _seed_wallet(db_session, fast_wallet)
        await _seed_wallet(db_session, slow_wallet)
        await db_session.commit()

        async def _fake_rate(wallet, limit=500):
            return {fast_wallet: 5000.0, slow_wallet: 50.0}[wallet]

        polled: list[str] = []

        async def _fake_poll_wallet(wallet_addr, watch_status, cutoff_ts):
            polled.append(wallet_addr)
            return {"found": 0, "in_window": 0, "skipped": 0, "inserted": 0, "new_sells": 0, "details": [], "aborted": False}

        monkeypatch.setattr(poll_mod, "_estimate_trades_per_day", _fake_rate)
        monkeypatch.setattr(poll_mod, "poll_wallet", _fake_poll_wallet)
        monkeypatch.setattr(poll_mod, "get_session_factory", lambda: session_factory)

        await run(lookback_days=7, max_trades_per_day=1000.0)

        assert polled == [slow_wallet], "the fast wallet must never reach poll_wallet"

    async def test_exclude_wallet_skips_without_probing_rate(
        self, db_session, session_factory, monkeypatch
    ):
        excluded_wallet = "0xexcludedwallet00000000000000000000000"
        kept_wallet = "0xkeptwallet0000000000000000000000000000"
        await _seed_wallet(db_session, excluded_wallet)
        await _seed_wallet(db_session, kept_wallet)
        await db_session.commit()

        probed: list[str] = []

        async def _fake_rate(wallet, limit=500):
            probed.append(wallet)
            return 1.0

        polled: list[str] = []

        async def _fake_poll_wallet(wallet_addr, watch_status, cutoff_ts):
            polled.append(wallet_addr)
            return {"found": 0, "in_window": 0, "skipped": 0, "inserted": 0, "new_sells": 0, "details": [], "aborted": False}

        monkeypatch.setattr(poll_mod, "_estimate_trades_per_day", _fake_rate)
        monkeypatch.setattr(poll_mod, "poll_wallet", _fake_poll_wallet)
        monkeypatch.setattr(poll_mod, "get_session_factory", lambda: session_factory)

        await run(lookback_days=7, exclude_wallets={excluded_wallet})

        assert polled == [kept_wallet]
        assert excluded_wallet not in probed, "excluded wallets should skip the rate probe entirely, not just poll_wallet"

    async def test_no_filter_polls_everyone(self, db_session, session_factory, monkeypatch):
        wallet_a = "0xwalleta0000000000000000000000000000000"
        wallet_b = "0xwalletb0000000000000000000000000000000"
        await _seed_wallet(db_session, wallet_a)
        await _seed_wallet(db_session, wallet_b)
        await db_session.commit()

        polled: list[str] = []

        async def _fake_poll_wallet(wallet_addr, watch_status, cutoff_ts):
            polled.append(wallet_addr)
            return {"found": 0, "in_window": 0, "skipped": 0, "inserted": 0, "new_sells": 0, "details": [], "aborted": False}

        monkeypatch.setattr(poll_mod, "poll_wallet", _fake_poll_wallet)
        monkeypatch.setattr(poll_mod, "get_session_factory", lambda: session_factory)

        await run(lookback_days=7)

        assert set(polled) == {wallet_a, wallet_b}


# ---------------------------------------------------------------------------
# run() — --wallet include list (item 16)
# ---------------------------------------------------------------------------

class TestIncludeWallets:
    async def test_wallet_flag_restricts_to_named_wallets_only(
        self, db_session, session_factory, monkeypatch
    ):
        """The whole watch_status universe must never be touched when
        --wallet is given -- only the named addresses are polled, and an
        address with no `wallets` row at all still works (labeled
        "unwatched")."""
        watched_but_not_named = "0xnotnamedwallet00000000000000000000000"
        named_and_unwatched = "0xnamedunwatchedwallet0000000000000000"
        await _seed_wallet(db_session, watched_but_not_named, watch_status="watch")
        await db_session.commit()  # named_and_unwatched deliberately has no Wallet row

        polled: list[tuple[str, str]] = []

        async def _fake_poll_wallet(wallet_addr, watch_status, cutoff_ts):
            polled.append((wallet_addr, watch_status))
            return {"found": 0, "in_window": 0, "skipped": 0, "inserted": 0, "new_sells": 0, "details": [], "aborted": False}

        monkeypatch.setattr(poll_mod, "poll_wallet", _fake_poll_wallet)
        monkeypatch.setattr(poll_mod, "get_session_factory", lambda: session_factory)

        await run(lookback_days=7, include_wallets={named_and_unwatched})

        assert polled == [(named_and_unwatched, "unwatched")]

    async def test_wallet_flag_bypasses_max_trades_per_day(
        self, db_session, session_factory, monkeypatch
    ):
        explicit_fast_wallet = "0xexplicitfastwallet00000000000000000000"
        await _seed_wallet(db_session, explicit_fast_wallet)
        await db_session.commit()

        probed: list[str] = []

        async def _fake_rate(wallet, limit=500):
            probed.append(wallet)
            return 999_999.0

        polled: list[str] = []

        async def _fake_poll_wallet(wallet_addr, watch_status, cutoff_ts):
            polled.append(wallet_addr)
            return {"found": 0, "in_window": 0, "skipped": 0, "inserted": 0, "new_sells": 0, "details": [], "aborted": False}

        monkeypatch.setattr(poll_mod, "_estimate_trades_per_day", _fake_rate)
        monkeypatch.setattr(poll_mod, "poll_wallet", _fake_poll_wallet)
        monkeypatch.setattr(poll_mod, "get_session_factory", lambda: session_factory)

        await run(lookback_days=7, max_trades_per_day=10.0, include_wallets={explicit_fast_wallet})

        assert polled == [explicit_fast_wallet]
        assert probed == [], "an explicit --wallet must never hit the rate probe"

    async def test_exclude_wallet_still_applies_on_top_of_wallet_flag(
        self, db_session, session_factory, monkeypatch
    ):
        wallet_a = "0xincludedwalleta0000000000000000000000"
        wallet_b = "0xincludedwalletb0000000000000000000000"
        await _seed_wallet(db_session, wallet_a)
        await _seed_wallet(db_session, wallet_b)
        await db_session.commit()

        polled: list[str] = []

        async def _fake_poll_wallet(wallet_addr, watch_status, cutoff_ts):
            polled.append(wallet_addr)
            return {"found": 0, "in_window": 0, "skipped": 0, "inserted": 0, "new_sells": 0, "details": [], "aborted": False}

        monkeypatch.setattr(poll_mod, "poll_wallet", _fake_poll_wallet)
        monkeypatch.setattr(poll_mod, "get_session_factory", lambda: session_factory)

        await run(
            lookback_days=7,
            include_wallets={wallet_a, wallet_b},
            exclude_wallets={wallet_a},
        )

        assert polled == [wallet_b]


# ---------------------------------------------------------------------------
# run() — [N of M] progress counter (item 16)
# ---------------------------------------------------------------------------

class TestProgressCounter:
    async def test_counter_reaches_m_even_when_every_wallet_is_skipped(
        self, db_session, session_factory, monkeypatch, capsys
    ):
        """The two skip paths must increment the counter too, or a run that
        skips wallets appears to end short of M (the kickoff's "16 of 26"
        drift bug)."""
        wallets = [f"0xctrwallet{i:031d}" for i in range(4)]
        for w in wallets:
            await _seed_wallet(db_session, w)
        await db_session.commit()

        async def _fake_rate(wallet, limit=500):
            return 5000.0  # over any realistic threshold below

        async def _fake_poll_wallet(wallet_addr, watch_status, cutoff_ts):
            return {"found": 0, "in_window": 0, "skipped": 0, "inserted": 0, "new_sells": 0, "details": [], "aborted": False}

        monkeypatch.setattr(poll_mod, "_estimate_trades_per_day", _fake_rate)
        monkeypatch.setattr(poll_mod, "poll_wallet", _fake_poll_wallet)
        monkeypatch.setattr(poll_mod, "get_session_factory", lambda: session_factory)

        # wallets[0] excluded outright, wallets[1:] rate-skipped -- every one
        # of the 4 is skipped by one path or the other, none reach poll_wallet.
        await run(
            lookback_days=7, max_trades_per_day=1000.0, exclude_wallets={wallets[0]},
        )

        out = capsys.readouterr().out
        assert "[1 of 4]" in out
        assert "[2 of 4]" in out
        assert "[3 of 4]" in out
        assert "[4 of 4]" in out  # counter reached M despite every wallet being skipped
        assert "(skipped so far: 4)" in out  # running tally reached the total


# ---------------------------------------------------------------------------
# run() — TRUNCATED surfaced in the end-of-run summary (item 16)
# ---------------------------------------------------------------------------

class TestTruncatedInSummary:
    async def test_truncated_wallet_appears_as_truncated_in_summary_table(
        self, db_session, session_factory, monkeypatch, capsys
    ):
        truncated_wallet = "0xtruncatedwallet0000000000000000000000"
        clean_wallet = "0xcleanwallet00000000000000000000000000"
        await _seed_wallet(db_session, truncated_wallet)
        await _seed_wallet(db_session, clean_wallet)
        await db_session.commit()

        async def _fake_poll_wallet(wallet_addr, watch_status, cutoff_ts):
            if wallet_addr == truncated_wallet:
                return {
                    "found": 500, "in_window": 500, "skipped": 0, "inserted": 200,
                    "new_sells": 0, "details": [], "aborted": False,
                    "truncated": True, "truncation_reason": "rate_limited",
                    "earliest_ts": None, "latest_ts": None,
                }
            return {
                "found": 3, "in_window": 3, "skipped": 0, "inserted": 3,
                "new_sells": 0, "details": [], "aborted": False,
                "truncated": False, "truncation_reason": None,
                "earliest_ts": None, "latest_ts": None,
            }

        monkeypatch.setattr(poll_mod, "poll_wallet", _fake_poll_wallet)
        monkeypatch.setattr(poll_mod, "get_session_factory", lambda: session_factory)

        await run(lookback_days=7)

        out = capsys.readouterr().out
        assert "RUN SUMMARY" in out
        assert "WARNING: 1 wallet(s) truncated this run" in out
        # The table row for the truncated wallet must show TRUNCATED as its
        # outcome -- a partial wallet must never look identical to a
        # complete one.
        summary_section = out[out.index("RUN SUMMARY"):]
        truncated_line = next(
            line for line in summary_section.splitlines() if line.startswith(truncated_wallet)
        )
        assert "TRUNCATED" in truncated_line
        clean_line = next(
            line for line in summary_section.splitlines() if line.startswith(clean_wallet)
        )
        assert "TRUNCATED" not in clean_line
        assert "polled" in clean_line
