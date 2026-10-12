"""Typed actionable insight produced from a persisted inbound email."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field


class InboxInsight(BaseModel):
    insight_id: str
    email_id: str
    operator_id: str = "default_operator"
    priority: Literal["low", "normal", "high", "urgent"] = "normal"
    priority_reasons: list[str] = Field(default_factory=list)
    action_required: bool = False
    request_summary: str | None = None
    suggested_action: str | None = None
    due_at: str | None = None
    proposed_owner: str | None = None
    related_entities: list[dict[str, Any]] = Field(default_factory=list)
    evidence_spans: list[str] = Field(default_factory=list)
    confidence: float = 0.0
    review_status: str = "not_required"
    category: str = "fyi"
    suppressed: bool = False
    suppression_reason: str | None = None
    dedup_key: str
    created_at: str | None = None
    updated_at: str | None = None
