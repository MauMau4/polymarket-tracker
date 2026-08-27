"""Polymarket Gamma API client — market and event discovery."""
import asyncio
import json as _json
import httpx
from tenacity import retry, stop_after_attempt, wait_exponential, retry_if_exception_type
from app.config import get_settings
from app.logging import get_logger
from app.schemas.market import MarketInfo, TokenInfo

logger = get_logger(__name__)

_PAGE_SIZE = 100
_MAX_PAGES = 50  # guard against runaway pagination


def _build_client() -> httpx.AsyncClient:
    settings = get_settings()
    return httpx.AsyncClient(
        base_url=settings.polymarket_gamma_base_url,
        timeout=30.0,
        headers={"Accept": "application/json"},
        verify=settings.gamma_ssl_verify,
    )


@retry(
    retry=retry_if_exception_type((httpx.HTTPStatusError, httpx.TransportError)),
    wait=wait_exponential(multiplier=1, min=1, max=60),
    stop=stop_after_attempt(5),
    reraise=True,
)
async def _get_keyset_page(
    client: httpx.AsyncClient, path: str, params: dict, log_offset: int
) -> dict:
    """
    Fetch one page from a Gamma /keyset list endpoint (markets or events).

    The bare /markets and /events endpoints are deprecated (sunset 2026-05-01,
    already past — confirmed live via the `deprecation`/`sunset` response
    headers) in favor of /markets/keyset and /events/keyset. Those reject the
    old `offset` param outright and require cursor-based paging via
    `after_cursor` (NOT `cursor` — that param name silently no-ops and
    re-returns page 1, a known upstream footgun). `log_offset` is just for
    log correlation, not sent to the API.
    """
    resp = await client.get(path, params=params)

    if resp.status_code == 429:
        logger.warning("gamma_rate_limited", path=path, offset=log_offset)
        raise httpx.HTTPStatusError("rate limited", request=resp.request, response=resp)

    resp.raise_for_status()
    data = resp.json()
    return data if isinstance(data, dict) else {}




# Map event ticker prefixes → human-readable category.
# The Gamma /markets endpoint does not return a category field; category only
# appears on the /events endpoint and is stripped from the embedded event objects.
# We infer it from the event ticker, which reliably starts with sport/topic name.
_TICKER_CATEGORY_MAP: dict[str, str] = {
    # Sports
    "nba": "Sports", "nfl": "Sports", "ufc": "Sports", "mlb": "Sports",
    "nhl": "Sports", "epl": "Sports", "mls": "Sports", "cfb": "Sports",
    "ncaa": "Sports", "f1": "Sports", "pga": "Sports", "golf": "Sports",
    "tennis": "Sports", "boxing": "Sports", "soccer": "Sports", "mma": "Sports",
    "fifa": "Sports", "rugby": "Sports", "cricket": "Sports", "formula": "Sports",
    "nascar": "Sports", "usl": "Sports", "champions-league": "Sports",
    # Crypto
    "bitcoin": "Crypto", "btc": "Crypto", "eth": "Crypto", "ethereum": "Crypto",
    "crypto": "Crypto", "sol": "Crypto", "solana": "Crypto", "xrp": "Crypto",
    "doge": "Crypto", "bnb": "Crypto", "avax": "Crypto", "ada": "Crypto",
    "defi": "Crypto", "nft": "Crypto",
    # Politics
    "trump": "Politics", "election": "Politics", "president": "Politics",
    "senate": "Politics", "congress": "Politics", "tariff": "Politics",
    "democrat": "Politics", "republican": "Politics", "vote": "Politics",
    "supreme-court": "Politics", "white-house": "Politics", "inauguration": "Politics",
    # Economics / Markets
    "fed": "Economics", "inflation": "Economics", "gdp": "Economics",
    "recession": "Economics", "interest-rate": "Economics", "sp500": "Economics",
    "nasdaq": "Economics", "dow": "Economics", "oil": "Economics",
    # World / Geopolitics
    "russia": "World", "ukraine": "World", "china": "World", "taiwan": "World",
    "war": "World", "nato": "World", "israel": "World", "iran": "World",
    # Science / Tech
    "ai": "Tech", "openai": "Tech", "apple": "Tech", "google": "Tech",
    "amazon": "Tech", "microsoft": "Tech", "tesla": "Tech", "spacex": "Tech",
    "elon": "Tech", "nasa": "Tech",
}


def _derive_category(raw: dict) -> str | None:
    """
    Derive a category from the event ticker embedded in a market response.
    The Gamma /markets API does not return category directly; this is a best-effort
    inference from the event ticker prefix (e.g. 'ufc-300-...' → 'Sports').
    """
    events = raw.get("events") or []
    ticker = None
    if events:
        ticker = events[0].get("ticker") or events[0].get("slug") or ""
    if not ticker:
        ticker = raw.get("slug") or ""
    ticker = ticker.lower()
    for prefix, category in _TICKER_CATEGORY_MAP.items():
        if ticker.startswith(prefix):
            return category
    return None


def _parse_json_list(raw_value) -> list:
    """Parse a Gamma field that arrives as a JSON-encoded string (e.g. outcomes)."""
    try:
        if isinstance(raw_value, str):
            return _json.loads(raw_value)
        return list(raw_value or [])
    except Exception:
        return []


_WINNER_PRICE_THRESHOLD = 0.99


def derive_resolution(raw: dict) -> tuple[bool, str | None]:
    """
    Derive (is_resolved, winning_outcome_label) from a raw Gamma market dict.

    The Gamma /markets endpoint has NO `resolved` or `resolution` fields.
    Settlement is signaled by closed=true plus degenerate outcomePrices: the
    winning outcome sits at ~1.0 (e.g. ["1", "0"]).

    Not treated as resolved:
      - closed markets whose prices are not yet degenerate (UMA settlement pending)
      - voided/refunded markets settling at ["0.5", "0.5"]
      - ancient markets returning ["0", "0"] (no price data)
    """
    if not raw.get("closed"):
        return False, None

    outcomes = _parse_json_list(raw.get("outcomes", "[]"))
    prices = _parse_json_list(raw.get("outcomePrices", "[]"))
    if not outcomes or len(prices) != len(outcomes):
        return False, None

    best_idx, best_price = -1, -1.0
    for i, p in enumerate(prices):
        try:
            fval = float(p)
        except (TypeError, ValueError):
            continue
        if fval > best_price:
            best_price, best_idx = fval, i

    if best_idx < 0 or best_price < _WINNER_PRICE_THRESHOLD:
        return False, None

    return True, str(outcomes[best_idx])


def _parse_gamma_datetime(value: str | None):
    """Parse a Gamma timestamp string (ISO 'T...Z' or Postgres-style space/'+00')."""
    from datetime import datetime as _dt

    if not value:
        return None
    try:
        return _dt.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def _parse_market(raw: dict) -> MarketInfo:
    token_ids: list = _parse_json_list(raw.get("clobTokenIds", "[]"))
    outcomes: list = _parse_json_list(raw.get("outcomes", "[]"))

    tokens = [
        TokenInfo(token_id=tid, outcome=outcomes[i] if i < len(outcomes) else None)
        for i, tid in enumerate(token_ids)
        if tid
    ]

    events = raw.get("events") or []
    event_title = events[0].get("title") if events else None
    if not event_title:
        event_title = raw.get("groupItemTitle")

    resolved, resolution = derive_resolution(raw)

    volume: float | None = None
    raw_vol = raw.get("volumeNum", raw.get("volume"))
    if raw_vol is not None:
        try:
            volume = float(raw_vol)
        except (TypeError, ValueError):
            volume = None

    return MarketInfo(
        market_id=raw.get("id", raw.get("market_id", "")),
        condition_id=raw.get("conditionId", raw.get("condition_id")),
        slug=raw.get("slug"),
        question=raw.get("question"),
        event_title=event_title,
        category=raw.get("category") or _derive_category(raw),
        active=raw.get("active", True),
        closed=raw.get("closed", False),
        resolved=resolved,
        resolution=resolution,
        end_date=raw.get("endDate", raw.get("end_date")),
        resolved_at=_parse_gamma_datetime(raw.get("closedTime")),
        volume=volume,
        neg_risk_market_id=raw.get("negRiskMarketID") if raw.get("negRisk") else None,
        tokens=tokens,
    )


async def fetch_market_by_slug(slug: str) -> MarketInfo | None:
    """
    Fetch a single market from Gamma by its slug (GET /markets/slug/{slug}).

    This is the only working path from a CLOB condition_id to a Gamma market:
    CLOB /markets/{condition_id} → market_slug → this lookup. The old
    query-param filters (?condition_id=, ?clob_token_ids=) are ignored by
    Gamma and return unrelated default listings.
    """
    if not slug:
        return None
    async with _build_client() as client:
        try:
            resp = await client.get(f"/markets/slug/{slug}")
            if resp.status_code in (404, 422):
                return None
            resp.raise_for_status()
            raw = resp.json()
            if not isinstance(raw, dict):
                return None
            return _parse_market(raw)
        except Exception as exc:
            logger.warning("gamma_market_slug_fetch_error", slug=slug, error=str(exc))
            return None


async def fetch_market_by_id(market_id: str) -> MarketInfo | None:
    """
    Fetch a single market from Gamma by its numeric ID (GET /markets/{id}).
    Returns None if the market is not found or the response is empty.
    """
    raw = await fetch_market_raw_by_id(market_id)
    if raw is None:
        logger.debug("gamma_market_not_found", market_id=market_id)
        return None
    try:
        parsed = _parse_market(raw)
    except Exception as exc:
        logger.warning("gamma_market_parse_error", market_id=market_id, error=str(exc))
        return None
    logger.debug(
        "gamma_market_fetched",
        market_id=market_id,
        closed=parsed.closed,
        resolved=parsed.resolved,
        resolution=parsed.resolution,
    )
    return parsed


async def fetch_resolved_markets(limit: int = 500) -> list[MarketInfo]:
    """
    Fetch recently resolved markets from the Gamma API.

    NOTE: This function is no longer used by the resolution job, which now performs
    per-market lookups via fetch_market_by_id. Kept for potential future use.
    """
    markets: list[MarketInfo] = []
    cursor: str | None = None
    async with _build_client() as client:
        for page in range(_MAX_PAGES):
            params: dict = {"closed": "true", "limit": _PAGE_SIZE}
            if cursor:
                params["after_cursor"] = cursor
            try:
                data = await _get_keyset_page(client, "/markets/keyset", params, page * _PAGE_SIZE)
            except Exception as exc:
                logger.warning("gamma_resolved_fetch_error", page=page, error=str(exc))
                break
            raw_markets = data.get("markets") or []
            if not raw_markets:
                break
            for raw in raw_markets:
                try:
                    parsed = _parse_market(raw)
                    if parsed.resolved and parsed.resolution:
                        markets.append(parsed)
                except Exception:
                    pass
            logger.debug(
                "gamma_resolved_page_fetched",
                page=page,
                raw_count=len(raw_markets),
                resolved_so_far=len(markets),
            )
            cursor = data.get("next_cursor")
            if len(markets) >= limit or not cursor or len(raw_markets) < _PAGE_SIZE:
                break
    logger.debug("gamma_resolved_markets_fetched", total=len(markets))
    return markets[:limit]


async def fetch_sports_markets_by_tag(tag_slug: str) -> list[MarketInfo]:
    """
    Fetch all active markets for a sport via the /events/keyset?tag_slug= endpoint.

    The /markets pagination only covers the first ~5,000 markets by ID, missing
    newer game markets (IDs 1M+). The /events endpoint with tag_slug filtering
    is the only reliable way to discover current sports game markets.

    Paginates until no more results (cursor-based — see _get_keyset_page).
    Returns MarketInfo objects in the same format as fetch_active_markets().
    """
    markets: list[MarketInfo] = []
    cursor: str | None = None

    async with _build_client() as client:
        for page in range(_MAX_PAGES):
            params: dict = {"tag_slug": tag_slug, "closed": "false", "limit": _PAGE_SIZE}
            if cursor:
                params["after_cursor"] = cursor
            try:
                data = await _get_keyset_page(client, "/events/keyset", params, page * _PAGE_SIZE)
            except Exception as exc:
                logger.warning(
                    "gamma_sports_tag_fetch_error",
                    tag_slug=tag_slug,
                    page=page,
                    error=str(exc),
                )
                break

            events = data.get("events") or []
            if not events:
                break

            for event in events:
                for raw_market in event.get("markets", []):
                    # Inject event-level fields that child markets lack
                    if not raw_market.get("events"):
                        raw_market = dict(raw_market)
                        raw_market["events"] = [event]
                    try:
                        markets.append(_parse_market(raw_market))
                    except Exception as exc:
                        logger.warning(
                            "gamma_sports_tag_parse_error",
                            tag_slug=tag_slug,
                            raw_id=raw_market.get("id"),
                            error=str(exc),
                        )

            cursor = data.get("next_cursor")
            if not cursor or len(events) < _PAGE_SIZE:
                break

    logger.info(
        "gamma_sports_tag_fetched",
        tag_slug=tag_slug,
        total_markets=len(markets),
    )
    return markets


async def fetch_active_markets(active_only: bool = True) -> list[MarketInfo]:
    """Fetch all active markets from the Gamma API with cursor pagination."""
    markets: list[MarketInfo] = []
    cursor: str | None = None
    async with _build_client() as client:
        for page in range(_MAX_PAGES):
            params: dict = {"limit": _PAGE_SIZE}
            if active_only:
                params["active"] = "true"
                params["closed"] = "false"
                params["archived"] = "false"
            if cursor:
                params["after_cursor"] = cursor
            try:
                data = await _get_keyset_page(client, "/markets/keyset", params, page * _PAGE_SIZE)
            except Exception as exc:
                logger.error("gamma_fetch_failed", page=page, error=str(exc))
                break

            raw_markets = data.get("markets") or []
            if not raw_markets:
                break

            for raw in raw_markets:
                try:
                    markets.append(_parse_market(raw))
                except Exception as exc:
                    logger.warning(
                        "gamma_parse_error",
                        raw_id=raw.get("id"),
                        error=str(exc),
                    )

            logger.debug("gamma_page_fetched", page=page, count=len(raw_markets))

            cursor = data.get("next_cursor")
            if not cursor or len(raw_markets) < _PAGE_SIZE:
                break

    logger.info("gamma_markets_fetched", total=len(markets))
    return markets


_BATCH_CONCURRENCY = 10


async def fetch_market_raw_by_id(market_id: str) -> dict | None:
    """
    Fetch one raw Gamma market dict via path-style GET /markets/{id}.

    NOTE: the query-param form (GET /markets?id=...) silently stopped matching
    and now returns [] for every ID — only the path form works. Unknown IDs
    return 422/404 → None. Never raises.
    """
    async with _build_client() as client:
        return await _fetch_market_raw_with_client(client, market_id)


async def _fetch_market_raw_with_client(client: httpx.AsyncClient, market_id: str) -> dict | None:
    for attempt, wait_secs in enumerate([0, 2, 5]):
        if wait_secs:
            await asyncio.sleep(wait_secs)
        try:
            resp = await client.get(f"/markets/{market_id}")
            if resp.status_code in (404, 422):
                return None
            if resp.status_code == 429:
                if attempt < 2:
                    continue
                logger.warning("gamma_market_rate_limited", market_id=market_id)
                return None
            resp.raise_for_status()
            data = resp.json()
            return data if isinstance(data, dict) else None
        except Exception as exc:
            logger.warning(
                "gamma_market_fetch_error",
                market_id=market_id,
                attempt=attempt,
                error=str(exc),
            )
            if attempt < 2:
                continue
            return None
    return None


async def fetch_markets_raw_by_ids(market_ids: list[str]) -> dict[str, dict]:
    """
    Fetch raw Gamma market dicts for many market IDs concurrently
    (path-style lookups, capped at _BATCH_CONCURRENCY in flight).
    Returns {market_id: raw_dict}; IDs Gamma no longer serves are absent.

    Never raises — failed lookups log a warning and contribute nothing.
    """
    if not market_ids:
        return {}

    results: dict[str, dict] = {}
    sem = asyncio.Semaphore(_BATCH_CONCURRENCY)

    async with _build_client() as client:
        async def _fetch_one(mid: str) -> None:
            async with sem:
                raw = await _fetch_market_raw_with_client(client, mid)
                if raw is not None:
                    results[mid] = raw

        await asyncio.gather(*(_fetch_one(mid) for mid in market_ids))

    logger.debug(
        "gamma_batch_fetch_complete",
        requested=len(market_ids),
        returned=len(results),
    )
    return results


async def fetch_market_prices_batch(
    market_ids: list[str],
    out_volumes: dict[str, float] | None = None,
) -> dict[str, dict[str, float]]:
    """
    Fetch current outcome prices for a batch of markets from the Gamma API.

    Uses concurrent path-style lookups via fetch_markets_raw_by_ids — the old
    repeated-id= query form silently stopped matching and returned [] for
    every request. Never raises; failed lookups return no prices.

    Returns {market_id: {outcome_label: price}}.
    If out_volumes is provided it is populated with {market_id: lifetime_volume_usd}.
    """
    if not market_ids:
        return {}

    raw_by_id = await fetch_markets_raw_by_ids(market_ids)

    results: dict[str, dict[str, float]] = {}
    for mid, raw in raw_by_id.items():
        outcomes = _parse_json_list(raw.get("outcomes", "[]"))
        out_prices = _parse_json_list(raw.get("outcomePrices", "[]"))
        market_prices: dict[str, float] = {}
        for i, outcome in enumerate(outcomes):
            if i < len(out_prices):
                try:
                    market_prices[str(outcome)] = float(out_prices[i])
                except (TypeError, ValueError):
                    pass
        if market_prices:
            results[mid] = market_prices
        if out_volumes is not None:
            raw_vol = raw.get("volumeNum") or raw.get("volume")
            if raw_vol is not None:
                try:
                    out_volumes[mid] = float(raw_vol)
                except (TypeError, ValueError):
                    pass

    logger.debug(
        "gamma_price_batch_complete",
        total_requested=len(market_ids),
        returned=len(results),
    )
    return results
