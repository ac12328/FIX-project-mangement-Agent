"""Typed meeting preparation snapshot contracts."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field


class MeetingPrepSnapshot(BaseModel):
    prep_id: str
    operator_id: str = "default_operator"
    calendar_event_id: str
    event_raw_hash: str
    project_id: str | None = None
    generated_at: str
    meeting: dict[str, Any]
    attendees: list[dict[str, Any]] = Field(default_factory=list)
    project_context: dict[str, Any] | None = None
    open_work_items: list[dict[str, Any]] = Field(default_factory=list)
    relevant_memories: list[dict[str, Any]] = Field(default_factory=list)
    recent_evidence: list[dict[str, Any]] = Field(default_factory=list)
    agenda: list[str] = Field(default_factory=list)
    open_decisions: list[str] = Field(default_factory=list)
    talking_points: list[str] = Field(default_factory=list)
    provenance: list[dict[str, str]] = Field(default_factory=list)
