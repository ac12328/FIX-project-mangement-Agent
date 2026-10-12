"""Typed contracts for deterministic learning governance."""
from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from src.learning.extraction.contracts import CandidateTarget, ScopeType, _validate_bounded_json


class CandidateLoadError(ValueError):
    """Raised when persisted candidate content cannot satisfy the typed contract."""


class CandidateRecord(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    candidate_id: str = Field(min_length=1)
    observation_id: str = Field(min_length=1)
    candidate_version: int = Field(ge=1)
    target_type: CandidateTarget
    subject_type: str = Field(min_length=1, max_length=80)
    subject_id: str | None = Field(default=None, max_length=200)
    predicate: str = Field(min_length=1, max_length=160)
    proposed_value: Any
    scope_type: ScopeType
    scope_id: str | None = Field(default=None, max_length=200)
    confidence: float = Field(ge=0.0, le=1.0)
    suggested_expiry: str | None
    reasoning_summary: str = Field(min_length=1, max_length=1000)
    risk_signals: list[str] = Field(max_length=20)
    evidence_refs: list[str] = Field(min_length=1, max_length=20)
    status: Literal["proposed", "needs_revision", "needs_review", "rejected", "quarantined"]
    extractor_version: str = Field(min_length=1)
    model_version: str = Field(min_length=1)
    prompt_version: str = Field(min_length=1)
    schema_version: str = Field(min_length=1)

    @model_validator(mode="after")
    def _bounded_value(self):
        _validate_bounded_json(self.proposed_value)
        return self


class ValidationFinding(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    code: str
    severity: Literal["low", "medium", "high", "critical"]
    message: str


class ValidationResult(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    validation_id: str
    candidate_id: str
    candidate_version: int
    validator_version: str
    policy_version: str
    policy_json: str
    passed: bool
    findings: list[ValidationFinding]
    risk_level: Literal["low", "medium", "high", "critical"]
    risk_score: int
    input_hash: str
    input_snapshot_json: str
    findings_hash: str
    disposition: Literal["needs_revision", "needs_review", "rejected", "quarantined"]
    requires_review: bool
    requires_evaluation: bool
    created_at: datetime


class CandidateRevision(BaseModel):
    """Proposal-only repair content; identity, evidence, and provenance are inherited."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    proposed_value: Any
    reasoning_summary: str = Field(min_length=1, max_length=1000)
    suggested_expiry: datetime | None = None
    predicate: str | None = Field(default=None, max_length=160)
    scope_type: str | None = None
    scope_id: str | None = None
    subject_type: str | None = None
    subject_id: str | None = None
    risk_signals: list[str] | None = None
