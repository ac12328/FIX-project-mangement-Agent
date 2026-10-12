"""Typed contracts for AI Workspace evaluations."""

from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Any, Literal

from pydantic import Field, field_validator, model_validator

from src.models.tool_arg_constraints import ConstraintOperator, EvalModel, ToolArgConstraint

DEFAULT_PASS_THRESHOLD = 85
PR_REPETITIONS = 1
PR_MINIMUM_PASSES = 1
NIGHTLY_CORE_REPETITIONS = 3
NIGHTLY_CORE_MINIMUM_PASSES = 2


class ArtifactLifecycle(str, Enum):
    MINED = "mined"
    QUESTION_APPROVED = "question_approved"
    GENERATED = "generated"
    EVIDENCE_READY = "evidence_ready"
    GOLDEN_READY = "golden_ready"
    VALIDATED = "validated"
    EVALUATED = "evaluated"
    REPORTED = "reported"
    FROZEN = "frozen"
    REJECTED = "rejected"
    RETIRED = "retired"


class SuiteKind(str, Enum):
    GENERATED = "generated"
    FROZEN_CI = "frozen_ci"
    LIVE_NIGHTLY = "live_nightly"


class FailureClass(str, Enum):
    FIXTURE_INVALID = "FIXTURE_INVALID"
    INFRA_OR_MODEL = "INFRA_OR_MODEL"
    TOOL_SELECTION = "TOOL_SELECTION"
    TOOL_ARGS_SCOPE = "TOOL_ARGS_SCOPE"
    TOOL_EXECUTION = "TOOL_EXECUTION"
    GROUNDING_CITATION = "GROUNDING_CITATION"
    REQUIRED_CLAIM_MISSING = "REQUIRED_CLAIM_MISSING"
    FORBIDDEN_OVERCLAIM = "FORBIDDEN_OVERCLAIM"
    SAFETY_GUARD_FALSE_POSITIVE = "SAFETY_GUARD_FALSE_POSITIVE"
    ANSWER_FORMAT = "ANSWER_FORMAT"
    JUDGE_INVALID = "JUDGE_INVALID"
    FLAKY = "FLAKY"


class LLMCallMetadata(EvalModel):
    role: Literal["question_miner", "question_generator", "golden_generator", "semantic_judge"]
    model: str
    prompt_version: str
    inference_trace_id: str | None = None
    output_revision_id: str | None = None


class ToolPlan(EvalModel):
    required_tool_counts: dict[str, int] = Field(default_factory=dict)
    args_constraints: list[ToolArgConstraint] = Field(default_factory=list)
    description: str = ""

    @field_validator("required_tool_counts")
    @classmethod
    def positive_counts(cls, value: dict[str, int]) -> dict[str, int]:
        if any(count < 1 for count in value.values()):
            raise ValueError("required tool counts must be positive")
        return value


class QuestionReviewStamp(EvalModel):
    reviewed_by: str = Field(min_length=1)
    reviewed_at: datetime
    decision: Literal["approve", "edit"]
    review_notes: str = ""
    original_question: str | None = None

    @field_validator("reviewed_at")
    @classmethod
    def require_aware_reviewed_at(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("question review timestamp must be timezone-aware")
        return value


class EvalCase(EvalModel):
    schema_version: Literal[1] = 1
    case_id: str = Field(pattern=r"^[a-z0-9][a-z0-9_-]{2,79}$")
    version: int = Field(default=1, ge=1)
    lifecycle: ArtifactLifecycle = ArtifactLifecycle.GENERATED
    question: str = Field(min_length=3)
    scope_mode: Literal["portfolio", "projects"] = "portfolio"
    project_ids: list[str] = Field(default_factory=list)
    web_enabled: bool = False
    expected_tool: str = Field(min_length=1)
    evidence_tool_args: dict[str, Any] = Field(default_factory=dict)
    expected_args_constraints: list[ToolArgConstraint] = Field(default_factory=list)
    alternative_tool_plans: list[ToolPlan] = Field(default_factory=list)
    as_of: datetime
    tags: list[str] = Field(default_factory=list)
    mining_batch_id: str | None = None
    pm_intent: str = ""
    pm_rationale: str = ""
    source_signal_ids: list[str] = Field(default_factory=list)
    question_review: QuestionReviewStamp | None = None
    pass_threshold: int = Field(default=DEFAULT_PASS_THRESHOLD, ge=0, le=100)
    repetitions: int = Field(default=3, ge=1, le=20)
    minimum_passes: int = Field(default=2, ge=1, le=20)
    critical_gates: list[str] = Field(default_factory=list)
    generator: LLMCallMetadata | None = None
    content_hash: str = ""

    @field_validator("as_of")
    @classmethod
    def require_aware_as_of(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("as_of must be timezone-aware")
        return value

    @field_validator("project_ids", "tags", "source_signal_ids")
    @classmethod
    def unique_strings(cls, value: list[str]) -> list[str]:
        return list(dict.fromkeys(item.strip() for item in value if item.strip()))

    @model_validator(mode="after")
    def validate_scope_and_repetitions(self) -> "EvalCase":
        if self.scope_mode == "projects" and not self.project_ids:
            raise ValueError("project-scoped cases require project_ids")
        if self.minimum_passes > self.repetitions:
            raise ValueError("minimum_passes cannot exceed repetitions")
        return self


class QuestionDraft(EvalModel):
    case_id: str = Field(pattern=r"^[a-z0-9][a-z0-9_-]{2,79}$")
    question: str = Field(min_length=3)
    scope_mode: Literal["portfolio", "projects"] = "portfolio"
    project_ids: list[str] = Field(default_factory=list)
    expected_tool: str
    evidence_tool_args: dict[str, Any] = Field(default_factory=dict)
    expected_args_constraints: list[ToolArgConstraint] = Field(default_factory=list)
    alternative_tool_plans: list[ToolPlan] = Field(default_factory=list)
    tags: list[str] = Field(default_factory=list)
    pm_intent: str = ""
    pm_rationale: str = ""
    source_signal_ids: list[str] = Field(default_factory=list)

    @field_validator("project_ids", "tags", "source_signal_ids")
    @classmethod
    def unique_question_strings(cls, value: list[str]) -> list[str]:
        return list(dict.fromkeys(item.strip() for item in value if item.strip()))

    @model_validator(mode="after")
    def validate_question_scope(self) -> "QuestionDraft":
        if self.scope_mode == "projects" and not self.project_ids:
            raise ValueError("project-scoped question drafts require project_ids")
        return self


class QuestionGenerationOutput(EvalModel):
    cases: list[QuestionDraft]


class EmailMiningSignal(EvalModel):
    signal_id: str = Field(min_length=3)
    kind: Literal["email_thread", "portfolio_email_pattern"] = "email_thread"
    project_id: str | None = None
    project_ids: list[str] = Field(default_factory=list)
    email_refs: list[str] = Field(default_factory=list)
    thread_key: str | None = None
    occurred_at: datetime | None = None
    latest_direction: str | None = None
    subject: str = ""
    summary: str = ""
    project_snapshot: dict[str, Any] = Field(default_factory=dict)
    metadata: dict[str, Any] = Field(default_factory=dict)

    @field_validator("occurred_at")
    @classmethod
    def require_aware_signal_time(cls, value: datetime | None) -> datetime | None:
        if value is not None and (value.tzinfo is None or value.utcoffset() is None):
            raise ValueError("mining signal timestamp must be timezone-aware")
        return value

    @field_validator("project_ids", "email_refs")
    @classmethod
    def unique_signal_strings(cls, value: list[str]) -> list[str]:
        return list(dict.fromkeys(item.strip() for item in value if item.strip()))


class QuestionMiningBatch(EvalModel):
    schema_version: Literal[1] = 1
    batch_id: str
    as_of: datetime
    created_at: datetime
    fixture_fingerprint: str
    signals: list[EmailMiningSignal]
    cases: list[EvalCase]
    holdout_against_suite_ids: list[str] = Field(default_factory=list)
    excluded_case_ids: list[str] = Field(default_factory=list)
    excluded_project_ids: list[str] = Field(default_factory=list)
    excluded_signal_ids: list[str] = Field(default_factory=list)
    generator: LLMCallMetadata | None = None
    content_hash: str = ""

    @field_validator("as_of", "created_at")
    @classmethod
    def require_aware_batch_time(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("question mining timestamps must be timezone-aware")
        return value

    @field_validator(
        "holdout_against_suite_ids",
        "excluded_case_ids",
        "excluded_project_ids",
        "excluded_signal_ids",
    )
    @classmethod
    def unique_holdout_strings(cls, value: list[str]) -> list[str]:
        return list(dict.fromkeys(item.strip() for item in value if item.strip()))


class QuestionReviewDecision(str, Enum):
    PENDING = "pending"
    APPROVE = "approve"
    EDIT = "edit"
    REJECT = "reject"


class QuestionReviewItem(EvalModel):
    case_id: str
    decision: QuestionReviewDecision = QuestionReviewDecision.PENDING
    edited_question: str | None = None
    notes: str = ""

    @model_validator(mode="after")
    def validate_edit(self) -> "QuestionReviewItem":
        if self.decision == QuestionReviewDecision.EDIT and not str(self.edited_question or "").strip():
            raise ValueError("edited_question is required when decision is edit")
        return self


class QuestionReviewBatch(EvalModel):
    schema_version: Literal[1] = 1
    batch_id: str
    source_content_hash: str
    reviewed_by: str = ""
    reviewed_at: datetime | None = None
    review_notes: str = ""
    decisions: list[QuestionReviewItem]
    content_hash: str = ""

    @field_validator("reviewed_at")
    @classmethod
    def require_aware_optional_review_time(cls, value: datetime | None) -> datetime | None:
        if value is not None and (value.tzinfo is None or value.utcoffset() is None):
            raise ValueError("reviewed_at must be timezone-aware")
        return value


class ApprovedQuestionBatch(EvalModel):
    schema_version: Literal[1] = 1
    batch_id: str
    source_content_hash: str
    review_content_hash: str
    reviewed_by: str
    reviewed_at: datetime
    review_notes: str = ""
    fixture_db_path: str | None = None
    cases: list[EvalCase]
    rejected_case_ids: list[str] = Field(default_factory=list)
    content_hash: str = ""

    @field_validator("reviewed_at")
    @classmethod
    def require_aware_approval_time(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("approved question timestamp must be timezone-aware")
        return value

class EvidenceSource(EvalModel):
    type: Literal["source"] = "source"
    source_id: str = Field(min_length=1)
    kind: str = Field(min_length=1)
    title: str = ""
    locator: str = ""
    snippet: str = ""
    project_id: str | None = None
    occurred_at: datetime | None = None
    url: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)

    @field_validator("occurred_at")
    @classmethod
    def require_aware_occurred_at(cls, value: datetime | None) -> datetime | None:
        if value is not None and (value.tzinfo is None or value.utcoffset() is None):
            raise ValueError("source occurred_at must be timezone-aware")
        return value


class EvidenceBundle(EvalModel):
    schema_version: Literal[1] = 1
    case_id: str
    case_version: int = Field(ge=1)
    captured_at: datetime
    data_watermark: datetime
    as_of: datetime
    tool_name: str
    tool_args: dict[str, Any] = Field(default_factory=dict)
    tool_result: dict[str, Any] = Field(default_factory=dict)
    sources: list[EvidenceSource] = Field(default_factory=list)
    redactions: list[str] = Field(default_factory=list)
    content_hash: str = ""

    @field_validator("captured_at", "data_watermark", "as_of")
    @classmethod
    def require_aware_datetime(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("evidence timestamps must be timezone-aware")
        return value

    @model_validator(mode="after")
    def validate_watermark_order(self) -> "EvidenceBundle":
        if self.data_watermark > self.captured_at:
            raise ValueError("data_watermark cannot be later than captured_at")
        return self


class AtomicClaim(EvalModel):
    claim_id: str = Field(pattern=r"^[a-z0-9][a-z0-9_-]{1,79}$")
    description: str = Field(min_length=3)
    supporting_source_ids: list[str] = Field(min_length=1)
    critical: bool = True


class ForbiddenClaim(EvalModel):
    claim_id: str = Field(pattern=r"^[a-z0-9][a-z0-9_-]{1,79}$")
    description: str = Field(min_length=3)
    patterns: list[str] = Field(default_factory=list)
    critical: bool = True


class GoldenSpec(EvalModel):
    schema_version: Literal[1] = 1
    case_id: str
    case_version: int = Field(ge=1)
    golden_answer: str = Field(min_length=3)
    required_claims: list[AtomicClaim] = Field(min_length=1)
    forbidden_claims: list[ForbiddenClaim] = Field(default_factory=list)
    acceptable_variants: list[str] = Field(default_factory=list)
    expected_tool: str
    expected_args_constraints: list[ToolArgConstraint] = Field(default_factory=list)
    alternative_tool_plans: list[ToolPlan] = Field(default_factory=list)
    judge_notes: str = ""
    generated_by: LLMCallMetadata | None = None
    content_hash: str = ""


class GateResult(EvalModel):
    gate_id: str
    passed: bool
    blocking: bool = True
    failure_class: FailureClass | None = None
    reason: str = ""
    details: dict[str, Any] = Field(default_factory=dict)


class CandidateObservation(EvalModel):
    case_id: str
    repetition: int = Field(default=1, ge=1)
    answer: str = ""
    actual_tools: list[str] = Field(default_factory=list)
    tool_trace: list[dict[str, Any]] = Field(default_factory=list)
    source_parts: list[dict[str, Any]] = Field(default_factory=list)
    validation_errors: list[str] = Field(default_factory=list)
    progress: list[dict[str, Any] | str] = Field(default_factory=list)
    elapsed_seconds: float = Field(default=0.0, ge=0)
    error: str | None = None
    agent_trace_id: str | None = None
    agent_run_id: str | None = None
    agent_turn_id: str | None = None


class RequiredClaimResult(EvalModel):
    claim_id: str
    passed: bool
    reason: str


class SemanticJudgeOutput(EvalModel):
    required_claim_results: list[RequiredClaimResult]
    forbidden_claim_violations: list[str] = Field(default_factory=list)
    citation_issues: list[str] = Field(default_factory=list)
    main_differences: list[str] = Field(default_factory=list)
    recommended_agent_fix: str = ""
    confidence: float = Field(default=1.0, ge=0.0, le=1.0)

class JudgeResult(EvalModel):
    case_id: str
    repetition: int = Field(default=1, ge=1)
    overall_pass: bool
    score_0_to_100: int = Field(ge=0, le=100)
    tool_selection_pass: bool
    tool_args_pass: bool
    grounding_pass: bool
    required_claim_results: list[RequiredClaimResult] = Field(default_factory=list)
    forbidden_claim_violations: list[str] = Field(default_factory=list)
    citation_issues: list[str] = Field(default_factory=list)
    main_differences: list[str] = Field(default_factory=list)
    recommended_agent_fix: str = ""
    deterministic_gates: list[GateResult] = Field(default_factory=list)
    failure_classes: list[FailureClass] = Field(default_factory=list)
    judge_model: str | None = None
    judge_error: str | None = None
    confidence: float = Field(default=1.0, ge=0.0, le=1.0)


class CaseRunResult(EvalModel):
    case_id: str
    question: str = ""
    pm_intent: str = ""
    pm_rationale: str = ""
    mining_batch_id: str | None = None
    source_signal_ids: list[str] = Field(default_factory=list)
    question_review: dict[str, Any] = Field(default_factory=dict)
    evidence: dict[str, Any] = Field(default_factory=dict)
    golden: dict[str, Any] = Field(default_factory=dict)
    candidates: list[CandidateObservation] = Field(default_factory=list)
    repetitions: list[JudgeResult]
    pass_count: int = Field(ge=0)
    minimum_passes: int = Field(ge=1)
    mean_score: float = Field(ge=0, le=100)
    overall_pass: bool
    flaky: bool = False


class EvalRunResult(EvalModel):
    schema_version: Literal[1] = 1
    run_id: str
    suite_id: str
    mode: Literal["offline", "candidate_report", "live"]
    started_at: datetime
    completed_at: datetime
    cases: list[CaseRunResult]
    passed_cases: int = Field(ge=0)
    total_cases: int = Field(ge=0)
    all_passed: bool
    artifact_hashes: dict[str, str] = Field(default_factory=dict)
    content_hash: str = ""


class LintIssue(EvalModel):
    code: str
    message: str
    path: str = ""
    blocking: bool = True


class PromotionResult(EvalModel):
    case_id: str
    version: int
    destination: str
    promoted: bool
    idempotent: bool = False
    artifact_hashes: dict[str, str] = Field(default_factory=dict)


class ReviewMetadata(EvalModel):
    schema_version: Literal[1] = 1
    case_id: str
    case_version: int = Field(ge=1)
    reviewed_by: str = Field(min_length=1)
    review_notes: str = Field(min_length=3)
    reviewed_at: datetime
    source_artifact_hashes: dict[str, str] = Field(default_factory=dict)
    content_hash: str = ""

    @field_validator("reviewed_at")
    @classmethod
    def require_aware_reviewed_at(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("reviewed_at must be timezone-aware")
        return value


class ArtifactRef(EvalModel):
    case_id: str
    version: int = Field(ge=1)
    path: str
    case_hash: str
    evidence_hash: str
    golden_hash: str


class SuiteManifest(EvalModel):
    schema_version: Literal[1] = 1
    suite_id: str
    kind: SuiteKind
    version: int = Field(default=1, ge=1)
    description: str = ""
    created_at: datetime
    cases: list[ArtifactRef]
    fixture_db_path: str | None = None
    repetitions: int = Field(default=PR_REPETITIONS, ge=1)
    minimum_passes: int = Field(default=PR_MINIMUM_PASSES, ge=1)
    pass_threshold: int = Field(default=DEFAULT_PASS_THRESHOLD, ge=0, le=100)
    content_hash: str = ""

    @model_validator(mode="before")
    @classmethod
    def apply_suite_policy(cls, value: Any) -> Any:
        if not isinstance(value, dict):
            return value
        data = dict(value)
        kind = data.get("kind")
        kind_value = kind.value if isinstance(kind, SuiteKind) else kind
        if kind_value == SuiteKind.FROZEN_CI.value:
            data.setdefault("repetitions", PR_REPETITIONS)
            data.setdefault("minimum_passes", PR_MINIMUM_PASSES)
        elif kind_value == SuiteKind.LIVE_NIGHTLY.value:
            data.setdefault("repetitions", NIGHTLY_CORE_REPETITIONS)
            data.setdefault("minimum_passes", NIGHTLY_CORE_MINIMUM_PASSES)
        return data

    @field_validator("created_at")
    @classmethod
    def require_aware_created_at(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("created_at must be timezone-aware")
        return value
