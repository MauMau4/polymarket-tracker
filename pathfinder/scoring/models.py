"""WalletScore result type (architecture §3.1 / §4)."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal


@dataclass(frozen=True)
class WalletScore:
    wallet: str
    as_of: datetime
    config_version: str
    qualified: bool
    mean_roi: Decimal | None
    win_rate: Decimal | None
    implied_win_rate: Decimal | None
    n_positions: int
    concentration_ratio: Decimal | None
    disqual_reasons: list[str] = field(default_factory=list)
