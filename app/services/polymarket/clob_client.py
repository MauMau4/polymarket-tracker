"""
Polymarket trade attribution via the public Data API.

The CLOB /trades endpoint is authenticated and user-scoped — it only returns
the API-key-holder's own trades, making it useless for market-wide attribution.
The Data API /trades endpoint is public, returns all market participants, and
includes the proxyWallet (L2 Polygon address) for each trade.

Attribution flow:
  WS event (asset_id, price, timestamp)
    → GET data-api.polymarket.com/trades?asset=<id>&startTs=<ts-60>&endTs=<ts+60>
    → match by price ± tolerance
    → return proxyWallet as the attributed wallet
"""
from datetime import datetime, timezone
from typing import Any

import httpx
from tenacity import retry, stop_after_attempt, wait_exponential, retry_if_exception_type

from app.config import get_settings
from app.logging import get_logger

logger = get_logger(__name__)

_DATA_API_BASE = "https://data-api.polymarket.com"
_TRADES_LIMIT = 100
# The WS last_trade_price event reflects the final fill price when a large order
# sweeps multiple price levels. Individual fills in the Data API may differ by
# up to ~2–3 cents from the broadcast price. 0.025 captures those while still
# avoiding cross-market false matches.
_PRICE_TOLERANCE = 0.025


def _build_data_client() -> httpx.AsyncClient:
    return httpx.AsyncClient(
        base_url=_DATA_API_BASE,
        timeout=15.0,
        headers={"Accept": "application/json"},
    )


class ClobTrade(dict):
    """
    Thin wrapper around a raw trade dict — supports both Data API and legacy CLOB shapes.

    Data API fields:  asset, proxyWallet, price, size, side, outcome, timestamp, transactionHash
    CLOB API fields:  asset_id, maker_address, price, size, side, outcome, match_time, transaction_hash

    Maker/taker detection: the public Data API /trades endpoint does not include a
    maker/taker type field. The authenticated CLOB API /trades endpoint returns a `type`
    field ("MAKER"/"TAKER") but is scoped to the API-key holder's own trades, making it
    useless for market-wide attribution. No maker/taker signal is available from the
    Data API and therefore not captured in the trade record.
    """

    @property
    def trade_id(self) -> str:
        return self.get("id", self.get("transactionHash", ""))

    @property
    def asset_id(self) -> str:
        return self.get("asset_id") or self.get("asset", "")

    @property
    def market(self) -> str:
        return self.get("market", self.get("conditionId", ""))

    @property
    def maker_address(self) -> str | None:
        # Data API uses proxyWallet; legacy CLOB used maker_address / owner
        return (
            self.get("proxyWallet")
            or self.get("maker_address")
            or self.get("owner")
        )

    @property
    def transaction_hash(self) -> str | None:
        return self.get("transactionHash") or self.get("transaction_hash")

    @property
    def price(self) -> float | None:
        raw = self.get("price")
        try:
            return float(raw) if raw is not None else None
        except (ValueError, TypeError):
            return None

    @property
    def size(self) -> float | None:
        raw = self.get("size")
        try:
            return float(raw) if raw is not None else None
        except (ValueError, TypeError):
            return None

    @property
    def side(self) -> str | None:
        return self.get("side")

    @property
    def outcome(self) -> str | None:
        return self.get("outcome")

    @property
    def match_time(self) -> datetime | None:
        # Data API: "timestamp" (seconds); CLOB: "match_time" (seconds)
        raw = self.get("timestamp") or self.get("match_time")
        if raw is None:
            return None
        try:
            value = float(raw)
            if value > 1e10:  # milliseconds guard, same as normalizer
                value /= 1000.0
            return datetime.fromtimestamp(value, tz=timezone.utc)
        except (ValueError, TypeError, OSError):
            return None


@retry(
    retry=retry_if_exception_type((httpx.HTTPStatusError, httpx.TransportError)),
    wait=wait_exponential(multiplier=1, min=1, max=30),
    stop=stop_after_attempt(3),
    reraise=True,
)
async def _fetch_data_api_trades(
    client: httpx.AsyncClient,
    asset_id: str,
    after_ts: int | None,
    before_ts: int | None,
) -> list[ClobTrade]:
    params: dict[str, Any] = {"asset": asset_id, "limit": _TRADES_LIMIT}
    if after_ts is not None:
        params["startTs"] = after_ts
    if before_ts is not None:
        params["endTs"] = before_ts

    resp = await client.get("/trades", params=params)

    if resp.status_code == 429:
        logger.warning("data_api_rate_limited", asset_id=asset_id[:20])
        raise httpx.HTTPStatusError("rate limited", request=resp.request, response=resp)

    resp.raise_for_status()
    data = resp.json()

    if isinstance(data, list):
        return [ClobTrade(t) for t in data]
    return [ClobTrade(t) for t in data.get("data", [])]


async def get_recent_trades(
    asset_id: str | None = None,
    market: str | None = None,
    after_ts: float | None = None,
    before_ts: float | None = None,
    max_pages: int = 1,
) -> list[ClobTrade]:
    """Fetch recent trades from the Data API for a given asset."""
    if not asset_id:
        logger.warning("get_recent_trades_called_without_asset_id")
        return []

    after_int = int(after_ts) if after_ts is not None else None
    before_int = int(before_ts) if before_ts is not None else None

    try:
        async with _build_data_client() as client:
            return await _fetch_data_api_trades(client, asset_id, after_int, before_int)
    except Exception as exc:
        logger.error("data_api_trades_fetch_failed", asset_id=asset_id[:20], error=str(exc))
        return []


async def verify_wallet_attribution(
    wallet: str,
    asset_id: str,
    price: float,
    event_ts: datetime,
    window_seconds: int = 120,
) -> bool:
    """
    Confirm that `wallet` has a matching trade for this asset near the event time.

    Called as a second-pass check after find_matching_trade() attributes a trade to
    a watched wallet.  Prevents misattribution when two wallets trade at the same
    price within the attribution window: the wallet-scoped endpoint can only return
    trades that actually belong to the specified wallet.

    Fails open (returns True) on timeout or HTTP error so a slow Data API never
    silently drops a real watched-wallet trade.
    """
    after_ts = int(event_ts.timestamp()) - window_seconds
    before_ts = int(event_ts.timestamp()) + window_seconds

    params: dict[str, Any] = {
        "user": wallet,
        "asset": asset_id,
        "startTs": after_ts,
        "endTs": before_ts,
        "limit": 20,
    }

    try:
        async with _build_data_client() as client:
            resp = await client.get("/trades", params=params)
            if resp.status_code == 429:
                logger.warning("verify_wallet_attribution_rate_limited", wallet=wallet[:12])
                return True  # fail open — don't discard on rate limit
            resp.raise_for_status()
            data = resp.json()
            trades = data if isinstance(data, list) else data.get("data", [])
            for t in trades:
                trade = ClobTrade(t)
                if trade.price is not None and abs(trade.price - price) <= _PRICE_TOLERANCE:
                    return True
            return False
    except Exception as exc:
        logger.warning("verify_wallet_attribution_error", wallet=wallet[:12], error=str(exc))
        return True  # fail open — don't discard on transient error


async def find_matching_trade(
    asset_id: str,
    price: float,
    event_ts: datetime,
    window_seconds: int = 60,
) -> ClobTrade | None:
    """
    Search the Data API for a trade matching the given asset/price near the event timestamp.
    Used as the primary wallet attribution path.

    The Data API is public (no auth) and returns proxyWallet for all market participants.
    """
    after_ts = int(event_ts.timestamp()) - window_seconds
    before_ts = int(event_ts.timestamp()) + window_seconds

    try:
        async with _build_data_client() as client:
            candidates = await _fetch_data_api_trades(client, asset_id, after_ts, before_ts)
    except Exception as exc:
        logger.error(
            "data_api_attribution_fetch_failed",
            asset_id=asset_id[:20],
            error=str(exc),
        )
        return None

    # Pick the candidate whose price is closest to the WS broadcast price.
    # The WS last_trade_price can differ from individual fill prices when a
    # single order sweeps multiple levels, so we take the nearest rather than
    # the first exact hit.
    within_tolerance = [
        t for t in candidates
        if t.price is not None and abs(t.price - price) <= _PRICE_TOLERANCE
    ]

    if not within_tolerance:
        logger.debug(
            "data_api_attribution_no_match",
            asset_id=asset_id[:20],
            price=price,
            window_seconds=window_seconds,
            candidates=len(candidates),
            closest_diff=min((abs(t.price - price) for t in candidates if t.price is not None), default=None),
        )
        return None

    best = min(within_tolerance, key=lambda t: abs(t.price - price))
    logger.debug(
        "data_api_attribution_match",
        asset_id=asset_id[:20],
        price=price,
        matched_price=best.price,
        diff=abs(best.price - price),
        wallet=best.maker_address,
    )
    return best
