"""HTTP Basic Auth middleware — protects all routes except /health."""
import base64
import secrets

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response

from app.logging import get_logger

logger = get_logger(__name__)

# Paths that bypass auth (health check for uptime monitors)
_PUBLIC_PATHS = {"/health", "/openapi.json", "/docs", "/redoc"}


class BasicAuthMiddleware(BaseHTTPMiddleware):
    """
    Enforces HTTP Basic Auth on all dashboard and API routes.
    Disabled automatically when DASHBOARD_PASSWORD is empty (dev mode).
    """

    def __init__(self, app, username: str, password: str) -> None:
        super().__init__(app)
        self._username = username
        self._password = password
        self._enabled = bool(password)
        if not self._enabled:
            logger.warning(
                "dashboard_auth_disabled",
                reason="DASHBOARD_PASSWORD not set — auth skipped in dev mode",
            )

    async def dispatch(self, request: Request, call_next):
        if not self._enabled:
            return await call_next(request)

        if request.url.path in _PUBLIC_PATHS:
            return await call_next(request)

        auth = request.headers.get("authorization", "")
        if not auth.lower().startswith("basic "):
            return Response(
                status_code=401,
                headers={"WWW-Authenticate": 'Basic realm="Polymarket Tracker"'},
            )

        try:
            decoded = base64.b64decode(auth[6:]).decode("utf-8", errors="replace")
            username, _, password = decoded.partition(":")
        except Exception:
            return Response(
                status_code=401,
                headers={"WWW-Authenticate": 'Basic realm="Polymarket Tracker"'},
            )

        valid = secrets.compare_digest(username, self._username) and secrets.compare_digest(
            password, self._password
        )
        if not valid:
            logger.warning("dashboard_auth_failed", path=request.url.path)
            return Response(
                status_code=401,
                headers={"WWW-Authenticate": 'Basic realm="Polymarket Tracker"'},
            )

        return await call_next(request)
