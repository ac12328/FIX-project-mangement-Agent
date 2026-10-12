"""Contracts and deterministic scoring for grounding-repair human review."""

from __future__ import annotations

import hashlib
import hmac
import json
import math
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from src.assistant.evals.artifacts import stamp_content_hash, verify_content_hash
from src.assistant.evals.grounding_repair import (
    GroundingRepairReviewBatch,
    GroundingRepairShadowCohort,
    verify_grounding_repair_cohort_content_hash,
)

RUBRIC_ID = "grounding-repair-human-review-v1"
PASS_THRESHOLD = 80.0
ISSUE_CODES = (
    "citation_id_error",
    "unsupported_claim",
    "scope_error",
    "as_of_error",
    "critical_claim_error",
)
CONTROLLED_SIMULATION_COLLECTION_CLASS = (
    "operator_authorized_controlled_live_production_workspace_simulation"
)


class ReviewWorkflowModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class RubricDimension(ReviewWorkflowModel):
    key: str
    label: str
    weight: float = Field(gt=0.0)


RUBRIC_DIMENSIONS = (
    RubricDimension(
        key="evidence_backed_factual_correctness",
        label="Evidence-backed factual correctness",
        weight=35.0,
    ),
    RubricDimension(
        key="citation_correctness_support",
        label="Citation correctness/support",
        weight=25.0,
    ),
    RubricDimension(key="completeness", label="Completeness", weight=20.0),
    RubricDimension(
        key="scope_as_of_correctness",
        label="Scope/as-of correctness",
        weight=10.0,
    ),
    RubricDimension(
        key="clarity_actionability",
        label="Clarity/actionability",
        weight=10.0,
    ),
)


class GroundingReviewRubric(ReviewWorkflowModel):
    schema_version: Literal[1] = 1
    rubric_id: Literal["grounding-repair-human-review-v1"] = RUBRIC_ID
    accepted_at: datetime
    acceptance_source: str
    cohort_id: str
    cohort_hash: str
    evidence_origin: Literal["controlled"] = "controlled"
    cohort_collection_class: str
    controlled_simulation_collection_class: Literal[
        "operator_authorized_controlled_live_production_workspace_simulation"
    ] = CONTROLLED_SIMULATION_COLLECTION_CLASS
    review_mode: Literal["blinded_randomized_answer_a_b"] = (
        "blinded_randomized_answer_a_b"
    )
    dimensions: list[RubricDimension]
    minimum_total_score: float = PASS_THRESHOLD
    requires_no_issue_codes: bool = True
    allowed_issue_codes: list[str]
    durable_note_policy: Literal["structured_scores_only_no_free_text"] = (
        "structured_scores_only_no_free_text"
    )
    content_hash: str = ""

    @field_validator("accepted_at")
    @classmethod
    def accepted_at_is_aware(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("accepted_at must be timezone-aware")
        return value

    @model_validator(mode="after")
    def rubric_is_fixed(self) -> "GroundingReviewRubric":
        if self.dimensions != list(RUBRIC_DIMENSIONS):
            raise ValueError("rubric dimensions or weights differ from the accepted rubric")
        if self.minimum_total_score != PASS_THRESHOLD or not self.requires_no_issue_codes:
            raise ValueError("rubric pass rule differs from the accepted rule")
        if self.allowed_issue_codes != list(ISSUE_CODES):
            raise ValueError("rubric issue-code set differs from the accepted set")
        return self


class ReviewAuditEntry(ReviewWorkflowModel):
    action: Literal["case_revealed", "review_submitted"]
    occurred_at: datetime
    reviewer: str
    case_id: str
    run_id: str
    event_id: str

    @field_validator("occurred_at")
    @classmethod
    def occurred_at_is_aware(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("occurred_at must be timezone-aware")
        return value


class ReviewAuditLedger(ReviewWorkflowModel):
    schema_version: Literal[1] = 1
    cohort_id: str
    cohort_hash: str
    rubric_hash: str
    created_at: datetime
    entries: list[ReviewAuditEntry] = Field(default_factory=list)
    content_hash: str = ""

    @field_validator("created_at")
    @classmethod
    def created_at_is_aware(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("created_at must be timezone-aware")
        return value


class BlindAssignment(str, Enum):
    CANDIDATE_IS_A = "candidate_is_a"
    BASELINE_IS_A = "baseline_is_a"


class ScoreResult(ReviewWorkflowModel):
    dimensions: dict[str, float]
    total: float
    passed: bool
    issue_codes: list[str]


class ReviewCaseView(ReviewWorkflowModel):
    case_id: str
    run_id: str
    position: int
    total: int
    completed: int
    user_request: str
    prepared_answer: str
    answer_a: str
    answer_b: str
    sources: list[Any]
    tool_results: list[Any]
    initial_errors: list[str]


class SubmissionResult(ReviewWorkflowModel):
    case_id: str
    assignment: BlindAssignment
    answer_a: ScoreResult
    answer_b: ScoreResult
    candidate: ScoreResult
    baseline: ScoreResult


def build_grounding_review_rubric(
    cohort: GroundingRepairShadowCohort,
    *,
    accepted_at: datetime | None = None,
) -> GroundingReviewRubric:
    if not verify_grounding_repair_cohort_content_hash(cohort):
        raise ValueError("cohort content hash is invalid")
    return stamp_content_hash(GroundingReviewRubric(
        accepted_at=accepted_at or datetime.now(timezone.utc),
        acceptance_source="Operator instruction: Use this rubric.",
        cohort_id=cohort.cohort_id,
        cohort_hash=cohort.content_hash,
        cohort_collection_class=str(cohort.collection_class or "unclassified"),
        dimensions=list(RUBRIC_DIMENSIONS),
        allowed_issue_codes=list(ISSUE_CODES),
    ))


def blinded_assignment(case_id: str, session_secret: str) -> BlindAssignment:
    if not case_id or not session_secret:
        raise ValueError("case_id and session_secret are required")
    digest = hmac.new(
        session_secret.encode("utf-8"), case_id.encode("utf-8"), hashlib.sha256
    ).digest()
    return (
        BlindAssignment.CANDIDATE_IS_A
        if digest[0] & 1
        else BlindAssignment.BASELINE_IS_A
    )


def score_answer(
    dimensions: dict[str, Any],
    issue_codes: list[str] | tuple[str, ...],
) -> ScoreResult:
    expected = {item.key: item for item in RUBRIC_DIMENSIONS}
    if set(dimensions) != set(expected):
        missing = sorted(set(expected) - set(dimensions))
        extra = sorted(set(dimensions) - set(expected))
        raise ValueError(f"rubric dimensions mismatch; missing={missing}, extra={extra}")
    normalized: dict[str, float] = {}
    for key, dimension in expected.items():
        try:
            value = float(dimensions[key])
        except (TypeError, ValueError) as exc:
            raise ValueError(f"{key} must be numeric") from exc
        if not math.isfinite(value) or value < 0.0 or value > dimension.weight:
            raise ValueError(f"{key} must be between 0 and {dimension.weight:g}")
        normalized[key] = value
    normalized_issues = list(dict.fromkeys(str(item).strip() for item in issue_codes))
    unknown = sorted(set(normalized_issues) - set(ISSUE_CODES))
    if unknown:
        raise ValueError(f"unknown review issue codes: {', '.join(unknown)}")
    total = round(sum(normalized.values()), 4)
    return ScoreResult(
        dimensions=normalized,
        total=total,
        passed=total >= PASS_THRESHOLD and not normalized_issues,
        issue_codes=normalized_issues,
    )


def answer_hash(value: str) -> str:
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, default=str).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def validate_review_lineage(
    cohort: GroundingRepairShadowCohort,
    batch: GroundingRepairReviewBatch,
) -> None:
    if not verify_grounding_repair_cohort_content_hash(cohort):
        raise ValueError("cohort content hash is invalid")
    if not verify_content_hash(batch):
        raise ValueError("review batch content hash is invalid")
    if batch.cohort_id != cohort.cohort_id or batch.cohort_hash != cohort.content_hash:
        raise ValueError("review batch does not reference this cohort")
    expected = {
        item.case_id: item
        for item in cohort.observations
        if item.data_complete and item.baseline_repair_attempted
    }
    actual = {item.case_id: item for item in batch.reviews}
    if len(actual) != len(batch.reviews):
        raise ValueError("review batch contains duplicate case IDs")
    if set(actual) != set(expected):
        raise ValueError("review batch case population does not match repair cohort")
    for case_id, review in actual.items():
        observation = expected[case_id]
        if review.candidate_answer_hash != observation.candidate_answer_hash:
            raise ValueError(f"candidate answer hash mismatch for {case_id}")
        if review.baseline_answer_hash != observation.baseline_answer_hash:
            raise ValueError(f"baseline answer hash mismatch for {case_id}")
