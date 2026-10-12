"""Pending review response schemas."""

from typing import Literal

from pydantic import BaseModel, Field


class ReviewItemResponse(BaseModel):
    update_id: str
    project_id: str
    proposed_changes: str
    task_type: str | None = None
    confidence: float = 0.0
    risk_level: str | None = None
    created_at: str | None = None
    client_name: str | None = None
    assessment_json: str | None = None
    policy_version: str | None = None
    source_type: str | None = None
    source_ref: str | None = None
    operator_id: str | None = None
    decided_by: str | None = None


class ReviewActionRequest(BaseModel):
    action: Literal["approve", "reject"]
    reject_tags: list[str] = Field(default_factory=list)
    reject_reason: str | None = None
