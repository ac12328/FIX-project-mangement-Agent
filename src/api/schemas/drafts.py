"""Draft response schemas."""

from pydantic import BaseModel, ConfigDict

from src.api.schemas.common import ActionResult
from src.models.enums import DraftStatus


class DraftResponse(BaseModel):
    model_config = ConfigDict(use_enum_values=True)

    draft_id: str
    project_id: str | None = None
    task_type: str | None = None
    status: DraftStatus
    subject: str | None = None
    current_draft_text: str | None = None
    draft_version: int = 1
    to_email: str | None = None
    cc: str | None = None
    ticket_id: str | None = None
    created_at: str | None = None
    # Thread context from the inbound email this draft replies to
    thread_subject: str | None = None
    thread_sender: str | None = None
    thread_cc: str | None = None


class DraftActionRequest(BaseModel):
    action: str  # approve | send | discard
    action_id: str | None = None


class DraftActionResult(ActionResult):
    action_id: str | None = None
