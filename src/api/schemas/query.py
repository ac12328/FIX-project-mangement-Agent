"""Aggregate query response schemas."""

from pydantic import BaseModel


class QueryDateRangeResponse(BaseModel):
    from_date: str | None = None
    to_date: str | None = None


class QueryScopeResponse(BaseModel):
    project_filter: str | None = None
    date_range: QueryDateRangeResponse
    contact_filter: str | None = None


class QueryContextProfileResponse(BaseModel):
    task_type: str
    query_text: str
    generated_at: str
    scope: QueryScopeResponse
    fields_requested: list[str] = []
    sort_order: str | None = None
    limit: int | None = None
    confidence: float
    escalate_to_operator: bool = False
    escalation_reason: str | None = None


class AggregateQueryRowResponse(BaseModel):
    project_id: str
    client_name: str | None = None
    market: str | None = None
    project_status: str | None = None
    ball_with: str | None = None
    current_milestone: str | None = None
    current_milestone_display: str | None = None
    next_action: str | None = None


class AggregateQueryResponse(BaseModel):
    status: str
    clarification_question: str | None = None
    profile: QueryContextProfileResponse
    suggested_filters: dict[str, str] = {}
    filters_applied: dict[str, str | bool] = {}
    total: int = 0
    results: list[AggregateQueryRowResponse] = []
