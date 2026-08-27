from datetime import datetime
from decimal import Decimal
from typing import Literal
from pydantic import BaseModel, Field


class NormalizedTrade(BaseModel):
    ts: datetime
    market_id: str
    asset_id: str
    outcome: str | None = None
    price: Decimal | None = None
    size: Decimal | None = None
    notional_usd: Decimal | None = None
    side: str | None = None
    wallet: str | None = None
    tx_hash: str | None = None
    attribution_status: Literal["attributed", "unresolved", "unattributable"] = "unresolved"
    trade_type: str | None = None
    source: str
    raw_payload: dict = Field(default_factory=dict)

    @property
    def is_attributed(self) -> bool:
        return self.attribution_status == "attributed"

    @property
    def notional_float(self) -> float | None:
        return float(self.notional_usd) if self.notional_usd is not None else None
