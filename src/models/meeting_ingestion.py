"""Typed contracts for evidence-backed meeting-note ingestion."""

from __future__ import annotations

from pydantic import BaseModel, Field


class MeetingExtractionItem(BaseModel):
    kind: str
    title: str
    description: str | None = None
    owner_type: str = "operator"
    owner_ref: str | None = None
    due_at: str | None = None
    evidence_spans: list[str] = Field(default_factory=list)
    confidence: float = 0.0


class MeetingIngestion(BaseModel):
    ingestion_id: str
    operator_id: str = "default_operator"
    calendar_event_id: str
    source_hash: str
    source_text: str
    extracted_items: list[MeetingExtractionItem] = Field(default_factory=list)
    work_item_ids: list[str] = Field(default_factory=list)
    created_at: str


class PreparedMeetingIngestion(BaseModel):
    """Validated extraction held outside a database write lease."""

    operator_id: str
    calendar_operator_id: str
    calendar_event_id: str
    source_hash: str
    source_text: str
    extracted_items: list[MeetingExtractionItem]
