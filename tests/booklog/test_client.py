"""pathfinder/booklog/client.py — no live network calls (respx-mocked httpx,
or direct monkeypatch of _post_books for the resilience test)."""
from decimal import Decimal

import httpx
import pytest
import respx

from pathfinder.booklog import client as booklog_client
from pathfinder.booklog.client import best_and_levels, fetch_books_batch

_BOOKS_URL = "https://clob.polymarket.com/books"


# ---------------------------------------------------------------------------
# best_and_levels — pure function, doesn't trust API ordering
# ---------------------------------------------------------------------------
def test_best_and_levels_bids_picks_max_regardless_of_input_order():
    # Deliberately out of order and not sorted — the real CLOB response's
    # ordering guarantee is undocumented, so the parser must not assume it.
    raw = [{"price": "0.40", "size": "10"}, {"price": "0.55", "size": "5"}, {"price": "0.10", "size": "100"}]
    best_price, best_size, levels = best_and_levels(raw, depth=3, best_is_max=True)
    assert best_price == Decimal("0.55")
    assert best_size == Decimal("5")
    assert [lvl["price"] for lvl in levels] == ["0.55", "0.40", "0.10"]


def test_best_and_levels_asks_picks_min():
    raw = [{"price": "0.60", "size": "10"}, {"price": "0.51", "size": "5"}, {"price": "0.90", "size": "100"}]
    best_price, best_size, levels = best_and_levels(raw, depth=3, best_is_max=False)
    assert best_price == Decimal("0.51")
    assert best_size == Decimal("5")
    assert [lvl["price"] for lvl in levels] == ["0.51", "0.60", "0.90"]


def test_best_and_levels_truncates_to_depth():
    raw = [{"price": str(p), "size": "1"} for p in ["0.1", "0.2", "0.3", "0.4", "0.5"]]
    _, _, levels = best_and_levels(raw, depth=3, best_is_max=True)
    assert len(levels) == 3
    assert [lvl["price"] for lvl in levels] == ["0.5", "0.4", "0.3"]


def test_best_and_levels_empty_side_returns_none():
    best_price, best_size, levels = best_and_levels([], depth=3, best_is_max=True)
    assert best_price is None
    assert best_size is None
    assert levels == []


# ---------------------------------------------------------------------------
# fetch_books_batch — respx-mocked HTTP
# ---------------------------------------------------------------------------
@respx.mock
async def test_fetch_books_batch_parses_by_asset_id():
    respx.post(_BOOKS_URL).mock(
        return_value=httpx.Response(
            200,
            json=[
                {"asset_id": "tok-a", "bids": [{"price": "0.4", "size": "10"}], "asks": [{"price": "0.6", "size": "20"}]},
                {"asset_id": "tok-b", "bids": [], "asks": []},
            ],
        )
    )

    result = await fetch_books_batch(["tok-a", "tok-b"])

    assert set(result.keys()) == {"tok-a", "tok-b"}
    assert result["tok-a"]["bids"][0]["price"] == "0.4"


@respx.mock
async def test_fetch_books_batch_missing_token_not_an_error():
    # Requested tok-c but the CLOB has nothing for it — simply absent, not an error.
    respx.post(_BOOKS_URL).mock(
        return_value=httpx.Response(200, json=[{"asset_id": "tok-a", "bids": [], "asks": []}])
    )

    result = await fetch_books_batch(["tok-a", "tok-c"])

    assert "tok-a" in result
    assert "tok-c" not in result


@respx.mock
async def test_fetch_books_batch_chunks_large_requests():
    call_sizes: list[int] = []

    def _responder(request: httpx.Request) -> httpx.Response:
        import json as _json

        body = _json.loads(request.content)
        call_sizes.append(len(body))
        return httpx.Response(200, json=[{"asset_id": item["token_id"], "bids": [], "asks": []} for item in body])

    respx.post(_BOOKS_URL).mock(side_effect=_responder)

    token_ids = [f"tok-{i}" for i in range(250)]
    result = await fetch_books_batch(token_ids)

    assert sorted(call_sizes) == [50, 100, 100]  # 250 split into batches of <=100
    assert len(result) == 250


async def test_fetch_books_batch_empty_input_makes_no_requests():
    assert await fetch_books_batch([]) == {}


# ---------------------------------------------------------------------------
# Resilience: one failing batch doesn't take down the others
# ---------------------------------------------------------------------------
async def test_one_batch_failure_does_not_lose_other_batches(monkeypatch):
    async def _fake_post_books(client, token_ids):
        if "bad" in token_ids:
            raise httpx.TransportError("connection reset")
        return [{"asset_id": tid, "bids": [], "asks": []} for tid in token_ids]

    monkeypatch.setattr(booklog_client, "_post_books", _fake_post_books)
    monkeypatch.setattr(booklog_client, "_BATCH_SIZE", 1)  # force one token_id per batch

    result = await fetch_books_batch(["good1", "bad", "good2"])

    assert set(result.keys()) == {"good1", "good2"}
