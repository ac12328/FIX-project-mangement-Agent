"""Typed read models for the additive Dashboard UI API v2."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field


SectionState = Literal["ok", "error"]
Priority = Literal["critical", "high", "medium", "low"]
Tone = Literal["neutral", "primary", "success", "warning", "danger"]


class ProblemResponse(BaseModel):
    type: str = "about:blank"
    title: str
    status: int
    detail: str
    instance: str | None = None


class SectionStatus(BaseModel):
    section: str
    state: SectionState
    message: str | None = None
    generated_at: str


class MetricReadModel(BaseModel):
    id: str
    label: str
    value: int | float | str
    detail: str
    tone: Tone = "neutral"
    href: str | None = None


class ActionItemReadModel(BaseModel):
    id: str
    kind: Literal["review", "draft", "approval", "followup"]
    priority: Priority
    status: str
    title: str
    reason: str
    project_id: str | None = None
    project_label: str | None = None
    created_at: str | None = None
    due_at: str | None = None
    age_days: int | None = None
    action_href: str
    action_label: str
    can_execute: bool = False


class RiskItemReadModel(BaseModel):
    project_id: str
    project_label: str
    health: Literal["red", "yellow", "green"]
    reason: str
    next_action: str | None = None
    milestone: str | None = None
    days_inactive: int | None = None


class RiskSummaryReadModel(BaseModel):
    red: int = 0
    yellow: int = 0
    green: int = 0
    items: list[RiskItemReadModel] = Field(default_factory=list)


class UpcomingItemReadModel(BaseModel):
    id: str
    kind: Literal["go_live", "followup"]
    title: str
    project_id: str
    project_label: str
    due_at: str
    days_until: int
    href: str


class ActionQueueCounts(BaseModel):
    review: int = Field(default=0, ge=0)
    draft: int = Field(default=0, ge=0)
    approval: int = Field(default=0, ge=0)
    followup: int = Field(default=0, ge=0)


class UiOverviewResponse(BaseModel):
    generated_at: str
    role: str
    can_execute: bool
    metrics: list[MetricReadModel] = Field(default_factory=list)
    action_queue: list[ActionItemReadModel] = Field(default_factory=list)
    action_queue_total: int = 0
    # None means unavailable, not an empty queue. Counts cover the full role-scoped queue.
    action_queue_counts: ActionQueueCounts | None = None
    risk_summary: RiskSummaryReadModel = Field(default_factory=RiskSummaryReadModel)
    upcoming: list[UpcomingItemReadModel] = Field(default_factory=list)
    section_status: list[SectionStatus] = Field(default_factory=list)


class SearchItemReadModel(BaseModel):
    id: str
    type: Literal["page", "project", "client", "vendor", "ticket"]
    title: str
    subtitle: str | None = None
    href: str
    keywords: list[str] = Field(default_factory=list)
    score: float = 0.0


class UiSearchResponse(BaseModel):
    items: list[SearchItemReadModel] = Field(default_factory=list)
    query: str
    generated_at: str


class FacetValue(BaseModel):
    value: str
    count: int


class FacetReadModel(BaseModel):
    id: str
    label: str
    values: list[FacetValue] = Field(default_factory=list)


class PaginatedReadModel(BaseModel):
    items: list[Any] = Field(default_factory=list)
    facets: list[FacetReadModel] = Field(default_factory=list)
    page: int
    page_size: int
    total: int
    generated_at: str


class WorkItemsResponse(PaginatedReadModel):
    items: list[ActionItemReadModel] = Field(default_factory=list)


class GoLiveDateReadModel(BaseModel):
    """Display projection; inferred dates never become confirmed actual dates."""
    go_live_date: str | None = None
    go_live_date_source: Literal["confirmed", "m8_inferred", "unknown"] = "unknown"
    go_live_date_source_ref: str | None = None
    go_live_date_source_at: str | None = Field(
        default=None, description="UTC timestamp of the selected M8 history record."
    )
    go_live_date_source_note: str | None = None


class ProjectListItemReadModel(GoLiveDateReadModel):
    project_id: str
    client_name: str | None = None
    market: str | None = None
    project_status: str
    ball_with: str
    current_milestone: str | None = None
    target_go_live: str | None = None
    actual_go_live: str | None = None
    actual_go_live_evidence_id: str | None = None
    actual_go_live_source_quote: str | None = None
    next_action: str | None = None
    updated_at: str | None = None
    last_activity_at: str | None = None
    operator_id: str = "default_operator"
    health: Literal["red", "yellow", "green"]
    days_stale: int = 0


class ProjectsResponse(PaginatedReadModel):
    items: list[ProjectListItemReadModel] = Field(default_factory=list)


class CompletedProjectListItemReadModel(BaseModel):
    project_id: str
    client_name: str | None = None
    market: str | None = None
    project_status: str
    ball_with: str
    current_milestone: str | None = None
    operator_id: str = "default_operator"
    completed_at: str
    days_since_completed: int = 0


class CompletedProjectsResponse(PaginatedReadModel):
    items: list[CompletedProjectListItemReadModel] = Field(default_factory=list)
    days: int = 30


class PartyListItemReadModel(BaseModel):
    org_id: str
    org_name: str
    org_type: str
    domains: list[str] = Field(default_factory=list)
    project_count: int = 0
    contact_count: int = 0
    created_at: str | None = None


class PartiesResponse(PaginatedReadModel):
    items: list[PartyListItemReadModel] = Field(default_factory=list)


class SupportListItemReadModel(BaseModel):
    ticket_id: str
    title: str
    display_title: str
    category: str
    severity: str
    status: str
    related_project_id: str | None = None
    related_project_name: str | None = None
    counterparty: str | None = None
    party_name: str | None = None
    reporter_email: str | None = None
    account_hint: str | None = None
    assigned_team: str | None = None
    updated_at: str | None = None
    last_activity_at: str | None = None
    last_sender: str | None = None
    last_subject: str | None = None
    last_preview: str | None = None
    age_days: int | None = None
    ball_with: str | None = None
    case_type: str = "other"
    queue: str = "needs_me"
    next_action: str = "review"
    on_project: bool = False
    is_noise: bool = False
    already_answered: bool = False
    mixed_thread: bool = False
    can_execute: bool = False


class SupportResponse(PaginatedReadModel):
    items: list[SupportListItemReadModel] = Field(default_factory=list)
    queue_counts: dict[str, int] = Field(default_factory=dict)


class DraftListItemReadModel(BaseModel):
    draft_id: str
    project_id: str | None = None
    project_label: str | None = None
    ticket_id: str | None = None
    task_type: str | None = None
    subject: str | None = None
    current_draft_text: str | None = None
    draft_version: int = 1
    to_email: str | None = None
    cc: str | None = None
    status: str
    review_state: str | None = None
    delivery_status: str | None = None
    scheduled_send_at: str | None = None
    created_at: str | None = None
    can_execute: bool = False


class DraftsResponse(PaginatedReadModel):
    items: list[DraftListItemReadModel] = Field(default_factory=list)


class ReviewListItemReadModel(BaseModel):
    update_id: str
    project_id: str
    project_label: str | None = None
    proposed_changes: str
    task_type: str | None = None
    confidence: float = 0.0
    risk_level: str | None = None
    created_at: str | None = None
    assessment_json: str | None = None
    policy_version: str | None = None
    source_type: str | None = None
    source_ref: str | None = None
    can_execute: bool = False


class ReviewsResponse(PaginatedReadModel):
    items: list[ReviewListItemReadModel] = Field(default_factory=list)


class ActivityListItemReadModel(BaseModel):
    id: int
    project_id: str | None = None
    actor: str | None = None
    surface: str | None = None
    action_type: str
    object_ref: str | None = None
    summary: str
    created_at: str


class ActivityResponse(PaginatedReadModel):
    items: list[ActivityListItemReadModel] = Field(default_factory=list)


class TimelineListItemReadModel(BaseModel):
    project_id: str
    client_name: str | None = None
    project_status: str
    current_milestone: str | None = None
    ball_with: str | None = None
    target_go_live: str | None = None
    project_tier: str | None = None
    health: Literal["red", "yellow", "green"]
    schedule_status: Literal["overdue", "due_soon", "scheduled", "no_date", "invalid"]
    days_to_go_live: int | None = None


class TimelineResponse(PaginatedReadModel):
    items: list[TimelineListItemReadModel] = Field(default_factory=list)
