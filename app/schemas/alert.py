from pydantic import BaseModel, Field


class AlertDecision(BaseModel):
    should_alert: bool
    severity: str
    alert_type: str
    dedupe_key: str
    reasons: list[str] = Field(default_factory=list)
    payload: dict = Field(default_factory=dict)
