"""Enriched support ticket detail response models."""

from pydantic import BaseModel, ConfigDict

from src.models.enums import BallWith, TicketCategory, TicketSeverity, TicketStatus


class ConversationMessage(BaseModel):
    """A single email/note in the ticket conversation thread."""
    evidence_id: str
    direction: str  # "inbound" | "outbound"
    sender_email: str | None = None
    sender_name: str | None = None
    subject: str | None = None
    preview: str  # first ~200 chars
    content_text: str | None = None
    timestamp: str
    evidence_type: str  # "email" | "note" | "log_snippet" | etc.
    is_latest: bool = False


class BallState(BaseModel):
    """Who has the ball — derived from ticket status + last evidence direction."""
    model_config = ConfigDict(use_enum_values=True)

    ball_with: BallWith
    since: str | None = None
    last_outbound_summary: str | None = None
    ball_reason: str | None = None


class FollowUpInfo(BaseModel):
    """Follow-up timer state for the ticket."""
    has_followup: bool = False
    followup_id: str | None = None
    due_date: str | None = None
    description: str | None = None


class ActivityEntry(BaseModel):
    """A single event in the merged activity timeline."""
    timestamp: str
    actor: str
    event_type: str  # "email_sent" | "email_received" | "status_change" | "ai_action" | "system"
    summary: str
    dot_color: str = "gray"  # "blue" | "purple" | "amber" | "gray"


class ProjectLink(BaseModel):
    """Related project info for the breadcrumb bar."""
    has_project: bool = False
    project_id: str | None = None
    project_name: str | None = None
    counterparty: str | None = None
    current_milestone: str | None = None


class AISummaryBlock(BaseModel):
    """AI-generated summary — same fields as existing, included inline."""
    from_party: str
    to_party: str
    what_happened: str


class TicketDetailResponse(BaseModel):
    """The single enriched payload for the PM detail view."""
    model_config = ConfigDict(use_enum_values=True)

    ticket_id: str
    title: str
    category: TicketCategory
    severity: TicketSeverity
    status: TicketStatus
    source_type: str | None = None
    counterparty: str | None = None
    counterparty_org_id: str | None = None
    counterparty_org_name: str | None = None
    reporter_email: str | None = None
    assigned_team: str | None = None
    escalated_to: str | None = None
    description: str | None = None
    root_cause: str | None = None
    resolution: str | None = None
    created_at: str | None = None
    updated_at: str | None = None
    ball: BallState
    conversation: list[ConversationMessage]
    followup: FollowUpInfo
    activity: list[ActivityEntry]
    project: ProjectLink
    ai_summary: AISummaryBlock | None = None
    reply_draft_text: str | None = None
