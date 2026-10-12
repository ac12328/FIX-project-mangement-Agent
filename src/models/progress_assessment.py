"""Typed evidence-grounded project-state assessment contracts."""

from __future__ import annotations

import re
from datetime import datetime
from enum import Enum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from src.models.enums import RiskLevel


class _StrictModel(BaseModel):
    # Enum values intentionally accept their JSON string representation. Primitive
    # safety-critical fields opt into strict validation individually.
    model_config = ConfigDict(extra="forbid")


class EvidenceSourceType(str, Enum):
    EMAIL = "email"
    CHAT = "chat"
    NOTE = "note"
    ATTACHMENT = "attachment"
    SYSTEM = "system"
    OPERATOR = "operator"


class EvidenceLevel(str, Enum):
    L1_OPERATOR_CONFIRMED = "L1_operator_confirmed"
    L2_AUTHORITATIVE_SYSTEM = "L2_authoritative_system"
    L3_NAMED_AUTHORITY_WRITTEN = "L3_named_authority_written"
    L4_TRUSTED_PARTY_WRITTEN = "L4_trusted_party_written"
    L5_UNVERIFIED_OR_INDIRECT = "L5_unverified_or_indirect"


class EvidencePolarity(str, Enum):
    POSITIVE = "positive"
    NEGATIVE = "negative"


class EvidenceModality(str, Enum):
    CONFIRMED = "confirmed"
    IN_PROGRESS = "in_progress"
    PLANNED = "planned"
    CONDITIONAL = "conditional"
    QUOTED_HISTORY = "quoted_history"
    UNKNOWN = "unknown"


class CriterionResult(str, Enum):
    MET = "met"
    MISSING = "missing"
    CONTRADICTED = "contradicted"
    NOT_APPLICABLE = "not_applicable"


class AssessmentDecision(str, Enum):
    ACCEPT_CANDIDATE = "accept_candidate"
    NEEDS_REVIEW = "needs_review"
    INSUFFICIENT_EVIDENCE = "insufficient_evidence"
    CONTRADICTION = "contradiction"
    CLARIFICATION_REQUIRED = "clarification_required"
    REJECT = "reject"


class AssessmentCode(str, Enum):
    INSUFFICIENT_EXIT_EVIDENCE = "INSUFFICIENT_EXIT_EVIDENCE"
    CONTRADICTORY_EVIDENCE = "CONTRADICTORY_EVIDENCE"
    AMBIGUOUS_PROJECT_IDENTITY = "AMBIGUOUS_PROJECT_IDENTITY"
    FUTURE_INTENT_NOT_COMPLETION = "FUTURE_INTENT_NOT_COMPLETION"
    CONDITIONAL_STATEMENT = "CONDITIONAL_STATEMENT"
    PARTIAL_COMPLETION = "PARTIAL_COMPLETION"
    UNAUTHORIZED_SOURCE = "UNAUTHORIZED_SOURCE"
    INVALID_STATE_TRANSITION = "INVALID_STATE_TRANSITION"
    TERMINAL_STATUS_REVIEW_REQUIRED = "TERMINAL_STATUS_REVIEW_REQUIRED"
    NON_STANDARD_PATH_REVIEW_REQUIRED = "NON_STANDARD_PATH_REVIEW_REQUIRED"


class ProjectStateField(str, Enum):
    CURRENT_MILESTONE = "current_milestone"
    PROJECT_STATUS = "project_status"
    BALL_WITH = "ball_with"
    NEXT_ACTION = "next_action"


class NextActionProvenance(str, Enum):
    EXPLICIT_COMMITMENT = "explicit_commitment"
    POLICY_DERIVED_FOLLOW_UP = "policy_derived_follow_up"
    GENERATED_SUGGESTION = "generated_suggestion"


class EvidenceCitation(_StrictModel):
    source_type: EvidenceSourceType
    source_ref: str = Field(min_length=1, max_length=500)
    quote: str = Field(min_length=1, max_length=4000)
    actor: str | None = Field(default=None, max_length=500)
    event_at: datetime | None = None
    evidence_level: EvidenceLevel
    polarity: EvidencePolarity = EvidencePolarity.POSITIVE
    modality: EvidenceModality = EvidenceModality.UNKNOWN

    @field_validator("source_ref", "quote", "actor")
    @classmethod
    def _strip_nonempty_text(cls, value: str | None) -> str | None:
        if value is None:
            return None
        stripped = value.strip()
        if not stripped:
            raise ValueError("value must not be blank")
        return stripped


class ExtractedProgressFact(_StrictModel):
    predicate: str = Field(min_length=1, max_length=200)
    value: str | bool | int | float | None = None
    subject: str | None = Field(default=None, max_length=500)
    citations: list[EvidenceCitation] = Field(default_factory=list)
    extraction_confidence: float = Field(ge=0.0, le=1.0)

    @field_validator("predicate")
    @classmethod
    def _validate_predicate(cls, value: str) -> str:
        stripped = value.strip()
        if not re.fullmatch(r"[a-z][a-z0-9_]*", stripped):
            raise ValueError("predicate must be stable snake_case")
        return stripped


class CriterionEvaluation(_StrictModel):
    criterion_id: str = Field(min_length=1, max_length=100)
    result: CriterionResult
    citations: list[EvidenceCitation] = Field(default_factory=list)
    explanation: str = Field(min_length=1, max_length=2000)
    codes: list[AssessmentCode] = Field(default_factory=list)

    @model_validator(mode="after")
    def _validate_citations(self) -> "CriterionEvaluation":
        if self.result == CriterionResult.MET and not self.citations:
            raise ValueError("met criterion requires a citation")
        if self.result == CriterionResult.CONTRADICTED:
            if not self.citations:
                raise ValueError("contradicted criterion requires a citation")
            if not any(c.polarity == EvidencePolarity.NEGATIVE for c in self.citations):
                raise ValueError("contradicted criterion requires negative evidence")
        return self


class ConfidenceVector(_StrictModel):
    extraction: float = Field(ge=0.0, le=1.0)
    project_identity: float = Field(ge=0.0, le=1.0)
    source_reliability: float = Field(ge=0.0, le=1.0)
    temporal_certainty: float = Field(ge=0.0, le=1.0)
    criteria_coverage: float = Field(ge=0.0, le=1.0)
    contradiction_score: float = Field(ge=0.0, le=1.0)


class FieldAssessment(_StrictModel):
    field: ProjectStateField
    current_value: str | None = None
    candidate_value: str | None = None
    decision: AssessmentDecision
    reasons: list[str] = Field(default_factory=list)
    codes: list[AssessmentCode] = Field(default_factory=list)
    risk_level: RiskLevel
    citations: list[EvidenceCitation] = Field(default_factory=list)

    @model_validator(mode="after")
    def _require_explanation(self) -> "FieldAssessment":
        self.reasons = [reason.strip() for reason in self.reasons if reason.strip()]
        if not self.reasons and not self.codes:
            raise ValueError("field assessment requires reasons or codes")
        return self


class NextActionAssessment(FieldAssessment):
    field: ProjectStateField = ProjectStateField.NEXT_ACTION
    provenance: NextActionProvenance
    accountable_actor: str | None = Field(default=None, max_length=200)
    due_at: datetime | None = None
    source_fact_predicates: list[str] = Field(default_factory=list)


class PctScopeException(_StrictModel):
    step_id: str = Field(min_length=1, max_length=100)
    scope_reason: str = Field(min_length=1, max_length=1000)
    source_ref: str = Field(min_length=1, max_length=500)
    approved_by: str = Field(min_length=1, max_length=500)
    citation: EvidenceCitation

    @field_validator("step_id")
    @classmethod
    def _validate_step_id(cls, value: str) -> str:
        stripped = value.strip()
        if not re.fullmatch(r"[a-z][a-z0-9_]*", stripped):
            raise ValueError("step_id must be stable snake_case")
        return stripped


class ProjectStateAssessment(_StrictModel):
    project_id: str | None = Field(default=None, max_length=200)
    source_type: EvidenceSourceType
    source_ref: str = Field(min_length=1, max_length=500)
    extracted_facts: list[ExtractedProgressFact] = Field(default_factory=list)
    milestone_criteria: list[CriterionEvaluation] = Field(default_factory=list)
    milestone_assessment: FieldAssessment | None = None
    status_assessment: FieldAssessment | None = None
    ball_with_assessment: FieldAssessment | None = None
    next_action_assessment: NextActionAssessment | None = None
    uncertainties: list[str] = Field(default_factory=list)
    contradictions: list[EvidenceCitation] = Field(default_factory=list)
    confidence: ConfidenceVector
    policy_version: str = Field(min_length=1, max_length=100)
    assessment_codes: list[AssessmentCode] = Field(default_factory=list)

    @field_validator("uncertainties")
    @classmethod
    def _normalize_uncertainties(cls, values: list[str]) -> list[str]:
        normalized = [value.strip() for value in values if value.strip()]
        return list(dict.fromkeys(normalized))

    @field_validator("source_ref", "project_id")
    @classmethod
    def _strip_text(cls, value: str | None) -> str | None:
        if value is None:
            return None
        stripped = value.strip()
        if not stripped:
            raise ValueError("value must not be blank")
        return stripped

    @model_validator(mode="after")
    def _validate_slots_and_identity(self) -> "ProjectStateAssessment":
        slots = {
            "milestone_assessment": ProjectStateField.CURRENT_MILESTONE,
            "status_assessment": ProjectStateField.PROJECT_STATUS,
            "ball_with_assessment": ProjectStateField.BALL_WITH,
            "next_action_assessment": ProjectStateField.NEXT_ACTION,
        }
        for name, expected in slots.items():
            assessment = getattr(self, name)
            if assessment is not None and assessment.field != expected:
                raise ValueError(f"{name} must assess {expected.value}")
        if (
            self.project_id is None
            and AssessmentCode.AMBIGUOUS_PROJECT_IDENTITY not in self.assessment_codes
        ):
            raise ValueError(
                "assessment without project_id requires AMBIGUOUS_PROJECT_IDENTITY"
            )
        return self

    def json_payload(self) -> dict[str, Any]:
        """Return a JSON-compatible payload for future audit persistence."""
        return self.model_dump(mode="json")
