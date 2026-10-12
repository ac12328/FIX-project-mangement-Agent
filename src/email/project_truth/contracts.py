"""Typed contracts for the Email -> Project Truth research pipeline."""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from src.models.followup_plan import ActionMutation

from src.models.enums import BallWith, Milestone, ProjectStatus
from src.models.progress_assessment import (
    EvidenceLevel,
    EvidenceModality,
    EvidencePolarity,
    EvidenceSourceType,
    EvidenceCitation,
    ExtractedProgressFact,
    ProjectStateAssessment,
)


@dataclass(frozen=True)
class IndependentStateCommandReceipt:
    """Source-bound command effect, not a promise of caller durability.

    Legacy tuple unpacking remains available to existing in-process consumers.
    A pending receipt must never be interpreted as a committed transaction.
    """

    project_id: str
    source_ref: str
    write_action: str
    status: Literal["persisted_in_caller_transaction", "committed", "not_applied", "incomplete"]
    proposed_fields: tuple[tuple[str, str], ...] = ()
    source_type: Literal["email"] = "email"
    evidence_id: str | None = None
    review_id: str | None = None
    update_id: str | None = None

    def __iter__(self):
        yield self.write_action
        yield dict(self.proposed_fields)


@dataclass(frozen=True)
class FieldRouteCommandReceipt:
    """A B field command's attributable effect inside its caller transaction."""

    project_id: str
    source_ref: str
    write_action: str
    status: Literal["persisted_in_caller_transaction", "not_applied", "incomplete"]
    proposed_fields: tuple[tuple[str, str], ...] = ()
    source_type: Literal["email"] = "email"
    evidence_id: str | None = None
    review_id: str | None = None
    update_id: str | None = None


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ProjectSnapshot(StrictModel):
    project_id: str
    current_milestone: Milestone | None = None
    project_status: ProjectStatus = ProjectStatus.ACTIVE
    ball_with: BallWith = BallWith.UNKNOWN
    next_action: str | None = None
    client_name: str | None = None
    snapshot_as_of: datetime | None = None
    reconstructed: bool = False
    action_plan: dict[str, Any] | None = None
    action_plan_truncated: bool = False


class EvidenceDocument(StrictModel):
    source_type: EvidenceSourceType
    source_ref: str = Field(min_length=1, max_length=500)
    text: str
    eligible_current_evidence: bool = True
    source_role: Literal["newest_authored_email", "attachment"]
    evidence_level: EvidenceLevel = EvidenceLevel.L5_UNVERIFIED_OR_INDIRECT
    actor: str | None = Field(default=None, max_length=500)
    event_at: datetime | None = None
    sha256: str = ""

    @model_validator(mode="after")
    def _derive_hash(self) -> "EvidenceDocument":
        if self.source_type not in {EvidenceSourceType.EMAIL, EvidenceSourceType.ATTACHMENT}:
            raise ValueError("email project-truth documents must be email or attachment")
        digest = hashlib.sha256(self.text.encode("utf-8")).hexdigest()
        if self.sha256 and self.sha256 != digest:
            raise ValueError("source hash does not match document text")
        self.sha256 = digest
        return self


class EmailProjectTruthCase(StrictModel):
    case_id: str
    source_ref: str
    project_id: str
    subject: str = ""
    sender_email: str = ""
    received_at: datetime | None = None
    authored_at: datetime | None = None
    newest_authored_body: str
    sources: list[EvidenceDocument]
    project_snapshot: ProjectSnapshot
    project_identity_resolved_hint: bool = True
    quoted_history: bool = False
    forwarded_attachment: bool = False
    unread_attachment_names: list[str] = Field(default_factory=list)
    partial_attachment_names: list[str] = Field(default_factory=list)
    synthetic: bool = False
    category: str = "production"

    @model_validator(mode="after")
    def _validate_sources(self) -> "EmailProjectTruthCase":
        refs = [source.source_ref for source in self.sources]
        if len(refs) != len(set(refs)):
            raise ValueError("case source references must be unique")
        if not any(source.source_type == EvidenceSourceType.EMAIL for source in self.sources):
            raise ValueError("case requires at least one email source")
        return self


class ExtractedEmailCitation(StrictModel):
    source_type: EvidenceSourceType
    source_ref: str = Field(min_length=1, max_length=500)
    quote: str = Field(min_length=1, max_length=4000)
    polarity: EvidencePolarity = EvidencePolarity.POSITIVE
    modality: EvidenceModality = EvidenceModality.UNKNOWN

    @field_validator("source_ref", "quote")
    @classmethod
    def _reject_blank(cls, value: str) -> str:
        if not value or not value.strip():
            raise ValueError("value must not be blank")
        return value


class ExtractedEmailFact(StrictModel):
    predicate: str = Field(min_length=1, max_length=200)
    value: str | bool | int | float | None = None
    subject: str | None = Field(default=None, max_length=500)
    citations: list[ExtractedEmailCitation] = Field(default_factory=list)
    extraction_confidence: float = Field(ge=0.0, le=1.0)

    @field_validator("predicate")
    @classmethod
    def _predicate(cls, value: str) -> str:
        if not re.fullmatch(r"[a-z][a-z0-9_]*", value):
            raise ValueError("predicate must be stable snake_case")
        return value


class EmailActionChange(StrictModel):
    """A proposed action mutation, not write authorization."""
    change: ActionMutation
    citations: list[ExtractedEmailCitation] = Field(min_length=1, max_length=10)
    extraction_confidence: float = Field(ge=0, le=1)


class EmailFactExtraction(StrictModel):
    project_identity_resolved: bool
    resolved_project_id: str | None = None
    facts: list[ExtractedEmailFact] = Field(default_factory=list)
    action_changes: list[EmailActionChange] = Field(default_factory=list, max_length=50)
    uncertainties: list[str] = Field(default_factory=list)
    contradiction_notes: list[str] = Field(default_factory=list)
    extraction_summary: str = ""

    @model_validator(mode="after")
    def _identity_consistency(self) -> "EmailFactExtraction":
        if self.project_identity_resolved and not self.resolved_project_id:
            raise ValueError("resolved identity requires resolved_project_id")
        if not self.project_identity_resolved:
            self.resolved_project_id = None
        return self


class CitationDiagnostic(StrictModel):
    fact_index: int
    citation_index: int
    source_ref: str
    code: Literal[
        "unknown_source",
        "source_type_mismatch",
        "quote_not_exact",
        "ineligible_current_source",
        "source_ref_repaired",
        "quote_repaired",
    ]
    detail: str


class ValidatedEmailEvidence(StrictModel):
    project_identity_resolved: bool
    resolved_project_id: str | None = None
    accepted_facts: list[ExtractedProgressFact] = Field(default_factory=list)
    rejected_citations: list[CitationDiagnostic] = Field(default_factory=list)
    source_hashes: dict[str, str] = Field(default_factory=dict)
    total_citations: int = 0
    accepted_citations: int = 0


class PipelineCallMetadata(StrictModel):
    model: str = ""
    latency_ms: int = 0
    trace_ids: list[str] = Field(default_factory=list)
    inference_trace_id: str | None = None
    output_revision_id: str | None = None
    attempts: int = 1
    error: str | None = None


class BaselineEmailRecommendation(StrictModel):
    stored_result: dict[str, Any] | None = None
    proposed_fields: dict[str, str] = Field(default_factory=dict)
    summary: str = ""
    suggested_action: str = ""
    answer: str = ""
    reply: str = ""
    round_count: int = 0
    tool_history: list[list[str]] = Field(default_factory=list)
    guard_results: list[dict[str, Any]] = Field(default_factory=list)
    exit_reason: str = "model_stop"
    call_metadata: PipelineCallMetadata = Field(default_factory=PipelineCallMetadata)


class EmailProjectRecommendation(StrictModel):
    assessment: ProjectStateAssessment
    proposed_fields: dict[str, str] = Field(default_factory=dict)
    state_change_citation_coverage: float = Field(ge=0.0, le=1.0)
    missing_evidence: list[str] = Field(default_factory=list)
    review_reasons: list[str] = Field(default_factory=list)
    summary: str
    suggested_action: str
    rendered_answer: str
    source_quotes: list[EvidenceCitation] = Field(default_factory=list)


class ReferenceRecommendation(StrictModel):
    project_identity_resolved: bool
    proposed_fields: dict[str, str] = Field(default_factory=dict)
    abstain: bool
    unsafe_update: bool = False
    summary: str
    suggested_action: str
    answer: str
    supported_source_refs: list[str] = Field(default_factory=list)
    reasoning_summary: list[str] = Field(default_factory=list)


class JudgeScore(StrictModel):
    factual_correctness: int = Field(ge=0, le=35)
    citation_support: int = Field(ge=0, le=25)
    completeness: int = Field(ge=0, le=20)
    scope_as_of: int = Field(ge=0, le=10)
    clarity_actionability: int = Field(ge=0, le=10)

    @property
    def total(self) -> int:
        return (
            self.factual_correctness
            + self.citation_support
            + self.completeness
            + self.scope_as_of
            + self.clarity_actionability
        )


class BlindJudgeResult(StrictModel):
    answer_a: JudgeScore
    answer_b: JudgeScore
    winner: Literal["A", "B", "tie"]
    explanation: str
    answer_a_unsafe: bool = False
    answer_b_unsafe: bool = False

    @model_validator(mode="after")
    def _enforce_tie_rule(self) -> "BlindJudgeResult":
        difference = self.answer_a.total - self.answer_b.total
        expected = "tie" if abs(difference) <= 2 else ("A" if difference > 0 else "B")
        self.winner = expected
        return self


class DeterministicCaseMetrics(StrictModel):
    field_correct: bool | None = None
    unsafe_update: bool = False
    correct_abstention: bool | None = None
    identity_handling: bool | None = None
    contradiction_handling: bool | None = None
    terminal_protected: bool | None = None
    support_non_regression: bool | None = None
    citation_coverage: float = 0.0


def jsonable(value: Any) -> Any:
    if isinstance(value, BaseModel):
        return value.model_dump(mode="json")
    return value
