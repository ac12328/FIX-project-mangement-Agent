"""
Support ticket model.
"""

from datetime import datetime

from pydantic import BaseModel, ConfigDict

from src.models.enums import (
    TicketCategory,
    TicketEvidenceType,
    TicketSeverity,
    TicketSourceType,
    TicketStatus,
)


class SupportTicket(BaseModel):
    """A support ticket for non-project technical issues."""
    ticket_id: str
    title: str
    category: TicketCategory
    severity: TicketSeverity = TicketSeverity.MEDIUM
    status: TicketStatus = TicketStatus.OPEN
    source_type: TicketSourceType = TicketSourceType.EMAIL
    related_project_id: str | None = None
    counterparty: str | None = None
    counterparty_org_id: str | None = None
    reporter_email: str | None = None
    reporter_contact_id: str | None = None
    description: str | None = None
    root_cause: str | None = None
    resolution: str | None = None
    assigned_team: str | None = None
    escalated_to: str | None = None
    reported_at: datetime | None = None
    resolved_at: datetime | None = None
    closed_at: datetime | None = None
    sla_target_hours: float | None = None
    resolution_pattern_id: str | None = None
    work_item_id: str | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None
    reply_draft_text: str | None = None
    draft_generated_at: datetime | None = None
    ai_summary_json: str | None = None


class SupportNotification(BaseModel):
    """Operator notification produced by support email routing."""
    profile: str
    title: str
    body: str
    project_id: str | None = None
    ticket_id: str
    advisory: dict | None = None


class TicketEvidence(BaseModel):
    """A piece of evidence attached to a support ticket."""
    model_config = ConfigDict(use_enum_values=True)

    evidence_id: str
    ticket_id: str
    evidence_type: TicketEvidenceType | str
    content_summary: str
    content_text: str | None = None
    source_ref: str | None = None
    created_at: datetime | None = None
