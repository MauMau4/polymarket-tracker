from functools import lru_cache
from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore", case_sensitive=False)

    app_env: str = "development"
    app_host: str = "0.0.0.0"
    app_port: int = 8000
    log_level: str = "INFO"

    database_url: str
    redis_url: str = "redis://localhost:6379/0"

    polymarket_gamma_base_url: str = "https://gamma-api.polymarket.com"
    polymarket_clob_base_url: str = "https://clob.polymarket.com"
    polymarket_data_api_base_url: str = "https://data-api.polymarket.com"
    polymarket_market_ws_url: str = "wss://ws-subscriptions-clob.polymarket.com/ws/market"

    polymarket_clob_api_key: str = ""
    polymarket_clob_api_secret: str = ""
    polymarket_clob_api_passphrase: str = ""

    alchemy_polygon_http_url: str = ""
    alchemy_polygon_ws_url: str = ""

    dune_api_key: str = ""

    telegram_bot_token: str = ""
    telegram_chat_id: str = ""

    dashboard_username: str = "admin"
    dashboard_password: str = ""

    default_whale_notional_usd: float = Field(default=10000.0)
    default_relative_liquidity_threshold: float = Field(default=0.05)
    default_alert_cooldown_seconds: int = Field(default=900)
    default_cluster_window_seconds: int = Field(default=300)
    default_watchlist_market_limit: int = Field(default=100)
    wallet_attribution_timeout_seconds: int = Field(default=10)
    trade_retention_days: int = Field(default=30)
    trade_minimum_notional_usd: float = Field(default=20.0)
    # Alert-fatigue reduction (decisions/2026-07-19.md item 1): minimum notional
    # for a real-time WATCH_WALLET ping from a non-digest watched wallet. Reuses
    # trade_minimum_notional_usd's value per the 2026-07-18 proposal's option 1
    # ("reuse ... trade_minimum_notional_usd"), kept as its own setting since the
    # two floors serve different purposes and may need to diverge later.
    watch_wallet_ping_floor_usd: float = Field(default=20.0)

    enable_alchemy_reconciliation: bool = False
    enable_dune_backfill: bool = False
    enable_cluster_detection: bool = True
    gamma_ssl_verify: bool = True

    # Volume-ranked subscription (decisions/2026-07-18.md coverage-fix proposal).
    # Off by default — flip on only after reviewing a projection report, since
    # this changes what trade data gets captured going forward.
    enable_volume_ranked_subscription: bool = False
    subscription_top_n: int = Field(default=1000)

    # Coverage-discovery genre expansion (decisions/2026-07-18.md proposal,
    # 2026-07-19.md item 4). Comma-separated genre names from
    # app.tasks.run_genre_discovery.GENRE_TAG_SLUGS, e.g. "politics,elections".
    # Empty by default — a genre only gets discovered/ingested once the
    # operator has approved its phase's projection report and added it here.
    enabled_genre_tags_csv: str = Field(default="")

    @property
    def enabled_genre_tags(self) -> list[str]:
        return [g.strip() for g in self.enabled_genre_tags_csv.split(",") if g.strip()]

    @property
    def is_production(self) -> bool:
        return self.app_env == "production"

    @property
    def clob_auth_configured(self) -> bool:
        return bool(self.polymarket_clob_api_key and self.polymarket_clob_api_secret)


@lru_cache
def get_settings() -> Settings:
    return Settings()
