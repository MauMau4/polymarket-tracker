"""pathfinder/booklog/snapshotter.py — real Postgres for candidate selection,
monkeypatched fetch_books_batch (no live network) for the write path."""
from decimal import Decimal

import pytest
from sqlalchemy import select

from app.db.models.book_snapshot import BookSnapshot
from pathfinder.booklog import snapshotter
from pathfinder.booklog.snapshotter import get_candidate_tokens, take_book_snapshot
from pathfinder.config import load_config
from tests.booklog.conftest import add_market, add_token

pytestmark = pytest.mark.asyncio

CONFIG = load_config()  # volume_floor_usd = 10000, book_depth_levels = 3


# ---------------------------------------------------------------------------
# get_candidate_tokens
# ---------------------------------------------------------------------------
async def test_below_volume_floor_excluded(db_session):
    await add_market(db_session, market_id="m1", volume=9999.0)
    await add_token(db_session, market_id="m1", asset_id="a1", outcome="Yes")

    candidates = await get_candidate_tokens(db_session, CONFIG)

    assert candidates == []


async def test_null_volume_excluded(db_session):
    await add_market(db_session, market_id="m2", volume=None)
    await add_token(db_session, market_id="m2", asset_id="a2", outcome="Yes")

    candidates = await get_candidate_tokens(db_session, CONFIG)

    assert candidates == []


async def test_resolved_market_excluded(db_session):
    await add_market(db_session, market_id="m3", volume=50000.0, resolved=True)
    await add_token(db_session, market_id="m3", asset_id="a3", outcome="Yes")

    candidates = await get_candidate_tokens(db_session, CONFIG)

    assert candidates == []


async def test_inactive_market_excluded(db_session):
    await add_market(db_session, market_id="m4", volume=50000.0, active=False)
    await add_token(db_session, market_id="m4", asset_id="a4", outcome="Yes")

    candidates = await get_candidate_tokens(db_session, CONFIG)

    assert candidates == []


async def test_eligible_market_prefers_yes_token(db_session):
    await add_market(db_session, market_id="m5", volume=50000.0)
    await add_token(db_session, market_id="m5", asset_id="a5-no", outcome="No")
    await add_token(db_session, market_id="m5", asset_id="a5-yes", outcome="Yes")

    candidates = await get_candidate_tokens(db_session, CONFIG)

    assert candidates == [("m5", "a5-yes")]


async def test_eligible_market_without_yes_label_falls_back_to_lowest_asset_id(db_session):
    # Categorical market: no token labeled 'Yes' at all.
    await add_market(db_session, market_id="m6", volume=50000.0)
    await add_token(db_session, market_id="m6", asset_id="zzz", outcome="Candidate B")
    await add_token(db_session, market_id="m6", asset_id="aaa", outcome="Candidate A")

    candidates = await get_candidate_tokens(db_session, CONFIG)

    assert candidates == [("m6", "aaa")]


async def test_at_exactly_volume_floor_is_included(db_session):
    await add_market(db_session, market_id="m7", volume=10000.0)
    await add_token(db_session, market_id="m7", asset_id="a7", outcome="Yes")

    candidates = await get_candidate_tokens(db_session, CONFIG)

    assert candidates == [("m7", "a7")]


def _config_with_watchlist(*titles: str):
    return CONFIG.model_copy(
        update={"booklog": CONFIG.booklog.model_copy(update={"watchlist_event_titles": list(titles)})}
    )


async def test_watchlisted_event_bypasses_volume_floor_even_when_zero(db_session):
    await add_market(db_session, market_id="m10", volume=0.0, event_title="MLB World Series Champion 2026")
    await add_token(db_session, market_id="m10", asset_id="a10", outcome="Yes")

    config = _config_with_watchlist("MLB World Series Champion 2026")
    candidates = await get_candidate_tokens(db_session, config)

    assert candidates == [("m10", "a10")]


async def test_watchlisted_event_bypasses_volume_floor_when_null(db_session):
    await add_market(db_session, market_id="m11", volume=None, event_title="World Cup Winner ")
    await add_token(db_session, market_id="m11", asset_id="a11", outcome="Yes")

    config = _config_with_watchlist("World Cup Winner ")
    candidates = await get_candidate_tokens(db_session, config)

    assert candidates == [("m11", "a11")]


async def test_non_watchlisted_event_still_subject_to_volume_floor(db_session):
    await add_market(db_session, market_id="m12", volume=500.0, event_title="Some Random Prop Market")
    await add_token(db_session, market_id="m12", asset_id="a12", outcome="Yes")

    config = _config_with_watchlist("MLB World Series Champion 2026")
    candidates = await get_candidate_tokens(db_session, config)

    assert candidates == []


async def test_empty_watchlist_falls_back_to_plain_volume_floor(db_session):
    await add_market(db_session, market_id="m13", volume=0.0, event_title="MLB World Series Champion 2026")
    await add_token(db_session, market_id="m13", asset_id="a13", outcome="Yes")

    config = _config_with_watchlist()  # explicitly empty
    candidates = await get_candidate_tokens(db_session, config)

    assert candidates == []


# ---------------------------------------------------------------------------
# take_book_snapshot — end to end, network mocked
# ---------------------------------------------------------------------------
async def test_take_book_snapshot_writes_parsed_row(db_session, session_factory, monkeypatch):
    await add_market(db_session, market_id="m8", volume=50000.0)
    await add_token(db_session, market_id="m8", asset_id="a8", outcome="Yes")
    await db_session.commit()

    async def _fake_fetch(asset_ids):
        assert asset_ids == ["a8"]
        return {
            "a8": {
                "asset_id": "a8",
                "bids": [{"price": "0.40", "size": "100"}, {"price": "0.39", "size": "50"}],
                "asks": [{"price": "0.42", "size": "80"}, {"price": "0.43", "size": "60"}],
            }
        }

    monkeypatch.setattr(snapshotter, "fetch_books_batch", _fake_fetch)

    result = await take_book_snapshot(CONFIG, session_factory)

    assert result == {"candidates": 1, "fetched": 1, "written": 1, "errors": 0}

    rows = (await db_session.execute(select(BookSnapshot).where(BookSnapshot.market_id == "m8"))).scalars().all()
    assert len(rows) == 1
    row = rows[0]
    assert row.best_bid == Decimal("0.40")
    assert row.best_ask == Decimal("0.42")
    assert row.bid_sz == Decimal("100")
    assert row.ask_sz == Decimal("80")
    assert len(row.levels["bids"]) == 2
    assert len(row.levels["asks"]) == 2


async def test_take_book_snapshot_no_candidates_writes_nothing(db_session, session_factory, monkeypatch):
    called = False

    async def _fake_fetch(asset_ids):
        nonlocal called
        called = True
        return {}

    monkeypatch.setattr(snapshotter, "fetch_books_batch", _fake_fetch)

    result = await take_book_snapshot(CONFIG, session_factory)

    assert result == {"candidates": 0, "fetched": 0, "written": 0, "errors": 0}
    assert called is False  # no HTTP call at all when there's nothing to fetch


async def test_take_book_snapshot_skips_token_missing_from_response(db_session, session_factory, monkeypatch):
    await add_market(db_session, market_id="m9", volume=50000.0)
    await add_token(db_session, market_id="m9", asset_id="a9", outcome="Yes")
    await db_session.commit()

    async def _fake_fetch(asset_ids):
        return {}  # CLOB has nothing for this token (e.g. delisted)

    monkeypatch.setattr(snapshotter, "fetch_books_batch", _fake_fetch)

    result = await take_book_snapshot(CONFIG, session_factory)

    assert result == {"candidates": 1, "fetched": 0, "written": 0, "errors": 0}
