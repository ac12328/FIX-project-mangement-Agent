"""
Evidence item model.
"""

from datetime import datetime

from pydantic import BaseModel

from src.models.enums import EvidenceType


class Evidence(BaseModel):
    """A single piece of evidence in the evidence store."""
    evidence_id: str
    project_id: str
    evidence_type: EvidenceType
    content_summary: str
    content_text: str | None = None
    source_ref: str | None = None
    evidence_at: datetime
    created_at: datetime | None = None
