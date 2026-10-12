"""
Activity log entry model.
"""

from datetime import datetime

from pydantic import BaseModel


class ActivityLogEntry(BaseModel):
    """A single entry in the unified activity log (append-only, immutable)."""
    id: int | None = None
    project_id: str | None = None
    actor: str
    surface: str | None = None
    action_type: str
    object_ref: str | None = None
    summary: str | None = None
    metadata_json: str | None = None
    created_at: datetime | None = None
