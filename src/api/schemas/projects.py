"""Project response schemas."""

from pydantic import BaseModel, ConfigDict, Field, field_validator

from src.api.schemas.chat import ChatRequest
from src.models.enums import BallWith, Milestone, ProjectStatus


class ProjectSummaryResponse(BaseModel):
    model_config = ConfigDict(use_enum_values=True)

    project_id: str
    client_name: str | None = None
    client_email: str | None = None
    market: str | None = None
    project_status: ProjectStatus
    ball_with: BallWith
    current_milestone: Milestone | None = None
    current_milestone_display: str | None = None
    target_go_live: str | None = None
    next_action: str | None = None
    updated_at: str | None = None
    last_activity_at: str | None = None
    operator_id: str = "default_operator"

    @field_validator("current_milestone", mode="before")
    @classmethod
    def empty_milestone_to_none(cls, v):
        if v is None or v == "":
            return None
        return v


class ProjectDetailResponse(ProjectSummaryResponse):
    broker_code: str | None = None
    connection_type: str | None = None
    notes: str | None = None
    logbook: str | None = None
    sla_limit: float | None = None
    operator_id: str = "default_operator"
    created_at: str | None = None


class ProjectStatusResponse(BaseModel):
    model_config = ConfigDict(use_enum_values=True)

    project_id: str
    client_name: str | None = None
    project_status: ProjectStatus | None = None
    current_milestone: Milestone | None = None
    ball_with: BallWith | None = None
    next_action: str | None = None
    market: str | None = None
    context_confidence: float | None = None
    pending_questions: list[str] = Field(default_factory=list)
    operator_instruction: str | None = None
    stale_memory_keys: list[str] = Field(default_factory=list)
    skill_observations: list[str] = Field(default_factory=list)
    skill_recommendations: list[str] = Field(default_factory=list)
    recent_evidence_count: int = 0
    llm_answer: str | None = None

    @field_validator(
        "project_status", "current_milestone", "ball_with", mode="before"
    )
    @classmethod
    def empty_optional_enum_to_none(cls, v):
        if v is None or v == "":
            return None
        return v


class ProjectNotesUpdate(BaseModel):
    notes: str


class ProjectBriefingEvent(BaseModel):
    source_ref: str
    occurred_at: str
    actor_name: str
    actor_role: str
    event_type: str
    summary: str


class ProjectSummaryAI(BaseModel):
    current_state: str
    blocker: str
    recommended_action: str
    next_action: str
    has_blocker: bool = False
    ball_with: BallWith
    events: list[ProjectBriefingEvent] = Field(default_factory=list)
    generated_at: str
    evidence_cutoff_at: str | None = None
    source_count: int = 0


class ProjectDraftSuggestion(BaseModel):
    model_config = ConfigDict(use_enum_values=True)

    draft_body: str
    ball_with: BallWith
    subject: str = ""


class ProjectChatRequest(ChatRequest):
    """Project chat request using the shared web-chat contract."""
