"""Typed personal task, commitment, waiting-for, and reminder record."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel

PersonalWorkKind = Literal["task", "commitment", "waiting_for", "reminder"]
ReviewStatus = Literal["proposed", "confirmed", "rejected", "not_required"]


class PersonalWorkItem(BaseModel):
    work_item_id: str
    title: str
    kind: PersonalWorkKind
    status: str
    priority: str
    operator_id: str
    description: str | None = None
    owner_type: str = "operator"
    owner_ref: str | None = None
    related_entity_type: str | None = None
    related_entity_id: str | None = None
    due_at: str | None = None
    planned_start_at: str | None = None
    duration_minutes: int | None = None
    remind_at: str | None = None
    snoozed_until: str | None = None
    recurrence_rule: str | None = None
    source_type: str = "manual"
    source_ref: str | None = None
    evidence_text: str | None = None
    confidence: float = 1.0
    review_status: ReviewStatus = "not_required"
    dedup_key: str | None = None
    completed_at: str | None = None
    resolution_note: str | None = None
    created_at: str | None = None
    updated_at: str | None = None
