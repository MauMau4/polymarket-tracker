from pydantic import BaseModel


class HealthResponse(BaseModel):
    status: str
    db: str
    redis: str
    ws_connected: bool
    last_ws_heartbeat: str | None = None


class ErrorResponse(BaseModel):
    detail: str
