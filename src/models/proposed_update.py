"""
ProposedUpdate and ProposedChange models.

Audit trail models for the proposed_updates table; live writes go through
safe_write_project() in write_helpers.py.
"""

import uuid
from datetime import datetime, timezone
from typing import Any

from pydantic import BaseModel, Field

from src.models.enums import RiskLevel, TaskType, WriteDecisionOutcome


class ProposedChange(BaseModel):
    """A single field change within a ProposedUpdate."""
    field: str
    old_value: str | None = None
    new_value: str | None
    reason: str
    evidence_ref: str | None = None


class ProposedUpdate(BaseModel):
    """A batch of proposed changes to project truth (legacy audit model).

    ``task_type`` is a business ``TaskType`` (write/draft/audit), not an
    ``LlmRoute`` key. See ``src.models.tasks``.
    """
    update_id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    project_id: str
    proposed_changes: list[ProposedChange] = Field(min_length=1)
    task_type: TaskType
    confidence: float = Field(ge=0.0, le=1.0)
    risk_level: RiskLevel
    requires_operator_review: bool = False
    operator_confirmed: bool = False
    assessment: dict[str, Any] | None = None
    policy_version: str | None = None
    source_type: str | None = None
    source_ref: str | None = None
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


class WriteDecision(BaseModel):
    """Result of a write layer decision (legacy audit model)."""
    update_id: str
    project_id: str
    outcome: WriteDecisionOutcome
    rejection_reasons: list[str] = Field(default_factory=list)
    changes_applied: list[ProposedChange] = Field(default_factory=list)
    changes_suppressed: list[ProposedChange] = Field(default_factory=list)
    decided_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
