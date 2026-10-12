"""Typed cross-module contracts for deterministic evaluation."""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class ReplayAction(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    action_type: str
    target: str = ""
    external: bool = False
    requires_confirmation: bool = False


class RenderedFact(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    value: str
    project_id: str | None
    source_ref: str


class ReplayRequest(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    case_id: str
    input_payload: dict[str, Any]
    candidate: dict[str, Any] | None = None


class ReplayResponse(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    text: str = ""
    recipient: str | None = None
    tone: str | None = None
    tool_calls: list[str] = Field(default_factory=list)
    project_ids: list[str] = Field(default_factory=list)
    contact_ids: list[str] = Field(default_factory=list)
    actions: list[ReplayAction] = Field(default_factory=list)
    rendered_facts: list[RenderedFact] = Field(default_factory=list)
    confirmation_required: bool = True
    policy_changes: list[str] = Field(default_factory=list)
    evidence_refs: list[str] = Field(default_factory=list)
    milestone_change: bool = False
    wiki_authoritative: bool = False
    contradiction_blocked: bool = False
    refused: bool = False


class AssertionResult(BaseModel):
    model_config = ConfigDict(frozen=True)
    code: str
    passed: bool
    detail: str = ""


class CaseOutcome(BaseModel):
    model_config = ConfigDict(frozen=True)
    case_id: str
    baseline_quality: float
    candidate_quality: float
    quality_delta: float
    passed: bool
    assertions: list[AssertionResult]


class ExpiryState(BaseModel):
    model_config = ConfigDict(frozen=True)
    expiry: datetime | None = None
    valid: bool
    expired: bool = False
    reason: str | None = None


def normalize_expiry(value: str | None, at: datetime) -> ExpiryState:
    if at.tzinfo is None:
        return ExpiryState(valid=False, reason="invalid_evaluation_time")
    if not value:
        return ExpiryState(valid=True)
    try:
        expiry = datetime.fromisoformat(value)
    except ValueError:
        return ExpiryState(valid=False, reason="invalid_expiry")
    if expiry.tzinfo is None:
        return ExpiryState(valid=False, reason="invalid_expiry")
    expiry = expiry.astimezone(timezone.utc)
    expired = expiry <= at.astimezone(timezone.utc)
    return ExpiryState(expiry=expiry, valid=True, expired=expired,
                       reason="candidate_expired" if expired else None)
