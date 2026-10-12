"""InboundReply model — Blueprint Section 27.3."""

from pydantic import BaseModel, Field


class InboundReply(BaseModel):
    """A detected client reply to a sent email, with Lead Agent analysis."""
    reply_id: str
    thread_id: str | None = None
    project_id: str
    from_contact_id: str | None = None
    received_at: str
    subject: str | None = None
    body_text: str | None = None
    original_draft_id: str | None = None
    summary: str | None = None
    suggested_action: str | None = None
    confidence: float = Field(default=0.0, ge=0.0, le=1.0)
    requested_docs: list[str] = []
