"""Polymarket CLOB order-book client (architecture §3.5, FR-5).

No order-book client existed anywhere in this codebase before Pathfinder —
this mirrors app/services/polymarket/gamma_client.py's httpx + tenacity
pattern (public endpoint, no auth needed for reads). The response contract
below (POST /books, {"token_id": ...} bodies, {market, asset_id, timestamp,
bids, asks} responses with bids/asks as {"price": str, "size": str} — prices
NOT guaranteed sorted best-first) was verified against the live CLOB API
this session, not assumed from memory.
"""
from __future__ import annotations

import asyncio
from decimal import Decimal

import httpx
from tenacity import retry, retry_if_exception_type, stop_after_attempt, wait_exponential

from app.config import get_settings
from app.logging import get_logger

logger = get_logger(__name__)

_BOOKS_PATH = "/books"
_BATCH_SIZE = 100
_BATCH_CONCURRENCY = 5


def _build_client() -> httpx.AsyncClient:
    settings = get_settings()
    return httpx.AsyncClient(
        base_url=settings.polymarket_clob_base_url,
        timeout=30.0,
        headers={"Accept": "application/json", "Content-Type": "application/json"},
        verify=settings.gamma_ssl_verify,
    )


@retry(
    retry=retry_if_exception_type((httpx.HTTPStatusError, httpx.TransportError)),
    wait=wait_exponential(multiplier=1, min=1, max=60),
    stop=stop_after_attempt(5),
    reraise=True,
)
async def _post_books(client: httpx.AsyncClient, token_ids: list[str]) -> list[dict]:
    resp = await client.post(_BOOKS_PATH, json=[{"token_id": tid} for tid in token_ids])
    if resp.status_code == 429:
        logger.warning("clob_books_rate_limited", batch_size=len(token_ids))
        raise httpx.HTTPStatusError("rate limited", request=resp.request, response=resp)
    resp.raise_for_status()
    data = resp.json()
    return data if isinstance(data, list) else []


async def fetch_books_batch(token_ids: list[str]) -> dict[str, dict]:
    """Fetch top-of-book for many token_ids in as few HTTP calls as possible.

    Returns {asset_id: raw_book_dict}. A token_id absent from the result means
    the CLOB had nothing for it (delisted/no liquidity) — not treated as an
    error; a failed batch (after retries) is logged and its token_ids are
    simply missing from the result rather than raising, so one bad batch
    doesn't take down the whole snapshot cycle.
    """
    if not token_ids:
        return {}

    batches = [token_ids[i : i + _BATCH_SIZE] for i in range(0, len(token_ids), _BATCH_SIZE)]
    semaphore = asyncio.Semaphore(_BATCH_CONCURRENCY)

    async def _fetch_one(client: httpx.AsyncClient, batch: list[str]) -> list[dict]:
        async with semaphore:
            try:
                return await _post_books(client, batch)
            except Exception as exc:
                logger.error("clob_books_batch_error", batch_size=len(batch), error=str(exc))
                return []

    async with _build_client() as client:
        batch_results = await asyncio.gather(*[_fetch_one(client, b) for b in batches])

    results: dict[str, dict] = {}
    for books in batch_results:
        for book in books:
            asset_id = book.get("asset_id")
            if asset_id:
                results[asset_id] = book
    return results


def best_and_levels(
    raw_side: list[dict], depth: int, *, best_is_max: bool
) -> tuple[Decimal | None, Decimal | None, list[dict]]:
    """Parse one side (bids or asks) of a raw book response.

    Does not trust API ordering (unverified/undocumented) — always sorts
    explicitly. `best_is_max=True` for bids (best = highest price a buyer
    will pay), `False` for asks (best = lowest price a seller will accept).
    Returns (best_price, best_size, top-`depth` levels best-to-worst).
    """
    if not raw_side:
        return None, None, []

    parsed = [
        (Decimal(str(level["price"])), Decimal(str(level["size"])))
        for level in raw_side
        if level.get("price") is not None and level.get("size") is not None
    ]
    if not parsed:
        return None, None, []

    parsed.sort(key=lambda pair: pair[0], reverse=best_is_max)
    top = parsed[:depth]
    best_price, best_size = top[0]
    levels = [{"price": str(price), "size": str(size)} for price, size in top]
    return best_price, best_size, levels
