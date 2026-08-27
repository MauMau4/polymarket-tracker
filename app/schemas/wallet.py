from pydantic import BaseModel


class ScoreComponents(BaseModel):
    """ROI-based score components stored in wallet_score_history.components."""
    weighted_log_roi: float = 0.0
    weighted_log_roi_normalized: float = 0.0
    consistency: float = 0.0
    consistency_normalized: float = 0.0
    win_rate: float = 0.0
    win_rate_normalized: float = 0.0
    closed_count: int = 0


# Alias kept so any code that still imports SkillScoreComponents doesn't
# hard-error during the transition window.
SkillScoreComponents = ScoreComponents
