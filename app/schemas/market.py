from datetime import datetime
from pydantic import BaseModel


class TokenInfo(BaseModel):
    token_id: str
    outcome: str | None = None


class MarketInfo(BaseModel):
    market_id: str
    condition_id: str | None = None
    slug: str | None = None
    question: str | None = None
    event_title: str | None = None
    category: str | None = None
    active: bool = True
    closed: bool = False
    resolved: bool = False
    resolution: str | None = None
    end_date: datetime | None = None
    # True settlement time (Gamma closedTime), distinct from end_date.
    resolved_at: datetime | None = None
    volume: float | None = None
    # Neg-risk combinatorial-market group id — see app/db/models/market.py.
    neg_risk_market_id: str | None = None
    tokens: list[TokenInfo] = []


class MarketDiscoveryResult(BaseModel):
    markets_upserted: int
    tokens_upserted: int
    active_asset_ids: list[str]
