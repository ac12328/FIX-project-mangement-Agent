"""Support ticket API response/request schemas."""

from pydantic import BaseModel, ConfigDict, Field

from src.api.schemas.chat import ChatRequest

from src.models.enums import (
    TicketCategory,
    TicketEvidenceType,
    TicketSeverity,
    TicketSourceType,
    TicketStatus,
)


class SupportTicketResponse(BaseModel):
    model_config = ConfigDict(use_enum_values=True)

    ticket_id: str
    title: str
    category: TicketCategory
    severity: TicketSeverity
    status: TicketStatus
    source_type: TicketSourceType | None = None
    related_project_id: str | None = None
    counterparty: str | None = None
    counterparty_org_id: str | None = None
    counterparty_org_name: str | None = None
    description: str | None = None
    root_cause: str | None = None
    resolution: str | None = None
    assigned_team: str | None = None
    escalated_to: str | None = None
    resolution_pattern_id: str | None = None
    created_at: str | None = None
    updated_at: str | None = None


class SupportTicketUpdateRequest(BaseModel):
    """PATCH body — invalid enum values yield 422."""

    model_config = ConfigDict(use_enum_values=True)

    status: TicketStatus | None = None
    severity: TicketSeverity | None = None
    assigned_team: str | None = None
    escalated_to: str | None = None
    root_cause: str | None = None
    resolution: str | None = None
    description: str | None = None
    category: TicketCategory | None = None
    counterparty: str | None = None
    related_project_id: str | None = None


class TicketEvidenceResponse(BaseModel):
    model_config = ConfigDict(use_enum_values=True)

    evidence_id: str
    ticket_id: str
    evidence_type: TicketEvidenceType
    content_summary: str
    content_text: str | None = None
    source_ref: str | None = None
    created_at: str | None = None


class SupportTicketActionRequest(BaseModel):
    action_type: str  # investigate | resolve | reopen | route
    params: dict = {}


class SupportChatRequest(ChatRequest):
    """Support chat request using the shared web-chat contract."""
