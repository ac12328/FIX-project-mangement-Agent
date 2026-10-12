"""Dashboard / Morning Brief response schemas."""

from pydantic import BaseModel, ConfigDict, field_validator

from src.models.enums import BallWith


class BriefItemResponse(BaseModel):
    model_config = ConfigDict(use_enum_values=True)

    project_id: str
    client_name: str | None = None
    current_milestone: str | None = None
    ball_with: BallWith | None = None
    next_action: str | None = None
    urgency: str
    urgency_reasons: list[str] = []
    days_inactive: int | None = None
    pending_drafts: int = 0
    open_followups: int = 0

    @field_validator("ball_with", mode="before")
    @classmethod
    def empty_ball_to_none(cls, v):
        if v is None or v == "":
            return None
        return v


class DashboardResponse(BaseModel):
    generated_at: str
    total_active: int
    red_items: list[BriefItemResponse] = []
    yellow_items: list[BriefItemResponse] = []
    green_items: list[BriefItemResponse] = []
    pending_reviews_count: int = 0
    pending_drafts_count: int = 0
    open_followups_count: int = 0
    overdue_followups_count: int = 0
    due_in_3_days_count: int = 0


class OverdueFollowupResponse(BaseModel):
    followup_id: str
    project_id: str
    client_name: str | None = None
    description: str
    due_date: str
    days_overdue: int


class ComingUpItem(BaseModel):
    project_id: str
    client_name: str | None = None
    label: str          # e.g. "Go-live target" or "Follow-up due"
    due_date: str       # YYYY-MM-DD
    days_until: int     # 0 = today, 1 = tomorrow, 2 = day after


class ComingUpResponse(BaseModel):
    today: list[ComingUpItem] = []
    tomorrow: list[ComingUpItem] = []
    day2: list[ComingUpItem] = []
