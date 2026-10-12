"""Portfolio health response models."""

from pydantic import BaseModel, ConfigDict, field_validator

from src.models.enums import BallWith, ProjectTier


class PortfolioProjectHealth(BaseModel):
    model_config = ConfigDict(use_enum_values=True)

    project_id: str
    client_name: str | None
    current_milestone: str | None
    ball_with: BallWith
    project_tier: ProjectTier | None = None
    days_stale: float
    health: str  # green | yellow | red
    pending_count: int
    # Team-owned internal_actions past deadline (status=open)
    overdue_internal_actions: int = 0
    # Operator internal_followups past due_date (status=open)
    overdue_internal_followups: int = 0

    @field_validator("project_tier", mode="before")
    @classmethod
    def empty_tier_to_none(cls, v):
        if v is None or v == "":
            return None
        return v

    @field_validator("current_milestone", mode="before")
    @classmethod
    def empty_milestone_to_none(cls, v):
        if v is None or v == "":
            return None
        return v


class PortfolioHealthResponse(BaseModel):
    projects: list[PortfolioProjectHealth]
    summary: dict  # {green: int, yellow: int, red: int}
