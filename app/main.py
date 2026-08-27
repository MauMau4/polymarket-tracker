from contextlib import asynccontextmanager
from fastapi import FastAPI
from app.api.middleware import BasicAuthMiddleware
from app.config import get_settings
from app.logging import setup_logging, get_logger
from app.api.routes import (
    health,
    markets,
    wallets,
    alerts,
    watchlists,
    price_level_alerts,
    settings as settings_router,
)
from app.ui import views as ui_views

setup_logging()
logger = get_logger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    cfg = get_settings()
    logger.info(
        "app_starting",
        env=cfg.app_env,
        whale_threshold_usd=cfg.default_whale_notional_usd,
        alchemy_enabled=cfg.enable_alchemy_reconciliation,
        dune_enabled=cfg.enable_dune_backfill,
    )
    yield
    logger.info("app_shutdown")


def create_app() -> FastAPI:
    cfg = get_settings()
    app = FastAPI(
        title="Polymarket Whale Tracker",
        version="0.1.0",
        docs_url="/docs" if not cfg.is_production else None,
        redoc_url=None,
        lifespan=lifespan,
    )

    # HTTP Basic Auth — protects all routes except /health (see middleware)
    app.add_middleware(
        BasicAuthMiddleware,
        username=cfg.dashboard_username,
        password=cfg.dashboard_password,
    )

    # JSON API routes
    app.include_router(health.router)
    app.include_router(markets.router, prefix="/api")
    app.include_router(wallets.router, prefix="/api")
    app.include_router(alerts.router, prefix="/api")
    app.include_router(watchlists.router, prefix="/api")
    app.include_router(price_level_alerts.router, prefix="/api")
    app.include_router(settings_router.router, prefix="/api")

    # HTML dashboard views
    app.include_router(ui_views.router)

    return app


app = create_app()
