"""Typed contracts for deterministic tool-argument and evidence replay evaluation."""

from __future__ import annotations

import re
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from src.models.tool_arg_constraints import ConstraintOperator, ToolArgConstraint


class ToolEvidenceEvalModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ToolArgumentExpectation(ToolEvidenceEvalModel):
    """Expected semantic arguments for one tool, satisfied by matching calls."""

    expectation_id: str = Field(pattern=r"^[a-z0-9][a-z0-9_-]{1,79}$")
    tool_name: str = Field(min_length=1)
    constraints: tuple[ToolArgConstraint, ...] = ()
    min_matching_calls: int = Field(default=1, ge=1)
    max_total_calls: int | None = Field(default=None, ge=1)
    critical: bool = True

    @model_validator(mode="after")
    def validate_call_bounds(self) -> "ToolArgumentExpectation":
        if self.max_total_calls is not None and self.max_total_calls < self.min_matching_calls:
            raise ValueError("max_total_calls cannot be less than min_matching_calls")
        return self


class ToolScopeRule(ToolEvidenceEvalModel):
    """Allow-list constraint for a scalar or collection-valued argument path."""

    rule_id: str = Field(pattern=r"^[a-z0-9][a-z0-9_-]{1,79}$")
    tool_name: str = Field(min_length=1)
    path: str = Field(min_length=1)
    allowed_values: tuple[str, ...] = Field(min_length=1)
    allow_missing: bool = False
    critical: bool = True

    @field_validator("allowed_values", mode="before")
    @classmethod
    def normalize_allowed_values(cls, value: Any) -> tuple[str, ...]:
        if isinstance(value, str):
            value = (value,)
        normalized = tuple(dict.fromkeys(str(item).strip() for item in value if str(item).strip()))
        if not normalized:
            raise ValueError("allowed_values must not be empty")
        return normalized


class EvidenceClaim(ToolEvidenceEvalModel):
    """Deterministic answer claim tied to source IDs emitted by tool results."""

    claim_id: str = Field(pattern=r"^[a-z0-9][a-z0-9_-]{1,79}$")
    description: str = Field(min_length=3)
    answer_patterns: tuple[str, ...] = Field(min_length=1)
    supporting_source_ids: tuple[str, ...] = Field(min_length=1)
    citation_mode: Literal["same_unit", "anywhere"] = "same_unit"
    critical: bool = True

    @field_validator("answer_patterns", mode="before")
    @classmethod
    def normalize_patterns(cls, value: Any) -> tuple[str, ...]:
        if isinstance(value, str):
            value = (value,)
        patterns = tuple(dict.fromkeys(str(item).strip() for item in value if str(item).strip()))
        if not patterns:
            raise ValueError("answer_patterns must not be empty")
        for pattern in patterns:
            try:
                re.compile(pattern)
            except re.error as exc:
                raise ValueError(f"invalid answer pattern: {pattern}") from exc
        return patterns

    @field_validator("supporting_source_ids", mode="before")
    @classmethod
    def normalize_source_ids(cls, value: Any) -> tuple[str, ...]:
        if isinstance(value, str):
            value = (value,)
        source_ids = tuple(dict.fromkeys(str(item).strip() for item in value if str(item).strip()))
        if not source_ids:
            raise ValueError("supporting_source_ids must not be empty")
        return source_ids


class ForbiddenAnswerClaim(ToolEvidenceEvalModel):
    claim_id: str = Field(pattern=r"^[a-z0-9][a-z0-9_-]{1,79}$")
    description: str = Field(min_length=3)
    patterns: tuple[str, ...] = Field(min_length=1)
    critical: bool = True

    @field_validator("patterns", mode="before")
    @classmethod
    def validate_patterns(cls, value: Any) -> tuple[str, ...]:
        if isinstance(value, str):
            value = (value,)
        patterns = tuple(dict.fromkeys(str(item).strip() for item in value if str(item).strip()))
        if not patterns:
            raise ValueError("patterns must not be empty")
        for pattern in patterns:
            try:
                re.compile(pattern)
            except re.error as exc:
                raise ValueError(f"invalid forbidden pattern: {pattern}") from exc
        return patterns


class ToolEvidenceEvalCase(ToolEvidenceEvalModel):
    """Frozen oracle for argument correctness and evidence-to-answer support."""

    schema_version: Literal[1] = 1
    case_id: str = Field(pattern=r"^[a-z0-9][a-z0-9_-]{2,79}$")
    user_request: str = Field(min_length=1)
    argument_expectations: tuple[ToolArgumentExpectation, ...] = ()
    scope_rules: tuple[ToolScopeRule, ...] = ()
    required_claims: tuple[EvidenceClaim, ...] = ()
    forbidden_claims: tuple[ForbiddenAnswerClaim, ...] = ()
    require_successful_results: bool = True
    require_final_answer: bool = True
    allow_unknown_citations: bool = False
    tags: tuple[str, ...] = ()

    @field_validator("tags", mode="before")
    @classmethod
    def normalize_tags(cls, value: Any) -> tuple[str, ...]:
        if value is None:
            return ()
        if isinstance(value, str):
            value = (value,)
        return tuple(dict.fromkeys(str(item).strip() for item in value if str(item).strip()))

    @model_validator(mode="after")
    def validate_unique_ids(self) -> "ToolEvidenceEvalCase":
        groups = (
            ("argument expectation", [item.expectation_id for item in self.argument_expectations]),
            ("scope rule", [item.rule_id for item in self.scope_rules]),
            ("claim", [item.claim_id for item in self.required_claims]),
            ("forbidden claim", [item.claim_id for item in self.forbidden_claims]),
        )
        for label, values in groups:
            if len(values) != len(set(values)):
                raise ValueError(f"duplicate {label} IDs are not allowed")
        return self


class ToolEvidenceTraceIssue(ToolEvidenceEvalModel):
    code: str
    severity: Literal["warning", "error"]
    message: str
    event_id: str | None = None
    tool_call_id: str | None = None


class ToolCallEvidence(ToolEvidenceEvalModel):
    tool_call_id: str
    tool_name: str
    arguments: dict[str, Any] | None = None
    result: Any = None
    status: str = "unknown"
    argument_payload_available: bool = False
    result_payload_available: bool = False
    start_event_id: str | None = None
    terminal_event_id: str | None = None
    source_ids: tuple[str, ...] = ()
    source_project_ids: dict[str, str] = Field(default_factory=dict)


class ToolEvidenceObservation(ToolEvidenceEvalModel):
    run_id: str | None = None
    trace_id: str | None = None
    tool_calls: tuple[ToolCallEvidence, ...] = ()
    final_answer: str = ""
    final_answer_available: bool = False
    cited_source_ids: tuple[str, ...] = ()
    available_source_ids: tuple[str, ...] = ()
    issues: tuple[ToolEvidenceTraceIssue, ...] = ()


class ArgumentConstraintResult(ToolEvidenceEvalModel):
    expectation_id: str
    tool_name: str
    tool_call_id: str | None = None
    path: str
    operator: ConstraintOperator
    expected: Any = None
    actual: Any = None
    actual_present: bool
    critical: bool
    passed: bool


class ToolArgumentExpectationResult(ToolEvidenceEvalModel):
    expectation_id: str
    tool_name: str
    call_count: int
    matching_call_count: int
    min_matching_calls: int
    max_total_calls: int | None = None
    passed: bool
    constraint_results: tuple[ArgumentConstraintResult, ...] = ()


class ScopeViolation(ToolEvidenceEvalModel):
    rule_id: str
    tool_name: str
    tool_call_id: str
    path: str
    actual_values: tuple[str, ...] = ()
    disallowed_values: tuple[str, ...] = ()
    missing: bool = False
    critical: bool = True


class EvidenceClaimResult(ToolEvidenceEvalModel):
    claim_id: str
    matched: bool
    evidence_available: bool
    cited_supporting_source_ids: tuple[str, ...] = ()
    missing_supporting_source_ids: tuple[str, ...] = ()
    supported: bool
    critical: bool


class ToolEvidenceCaseResult(ToolEvidenceEvalModel):
    case_id: str
    run_id: str | None = None
    argument_constraint_count: int
    argument_constraint_pass_count: int
    argument_constraint_pass_rate: float
    missing_required_argument_count: int
    expectation_pass_count: int
    expectation_count: int
    scope_overreach_count: int
    successful_result_count: int
    required_result_count: int
    tool_result_availability_rate: float
    cited_source_count: int
    known_cited_source_count: int
    unknown_citations: tuple[str, ...] = ()
    cited_source_coverage: float
    required_claim_count: int
    supported_claim_count: int
    required_claim_support_rate: float
    unsupported_claim_ids: tuple[str, ...] = ()
    forbidden_claim_violations: tuple[str, ...] = ()
    trace_sufficient: bool
    arguments_pass: bool
    scope_pass: bool
    tool_results_pass: bool
    citations_pass: bool
    claims_pass: bool
    forbidden_claims_pass: bool
    passed: bool
    findings: tuple[str, ...] = ()
    expectation_results: tuple[ToolArgumentExpectationResult, ...] = ()
    scope_violations: tuple[ScopeViolation, ...] = ()
    claim_results: tuple[EvidenceClaimResult, ...] = ()
    observation: ToolEvidenceObservation


class ToolEvidenceSuiteResult(ToolEvidenceEvalModel):
    schema_version: Literal[1] = 1
    case_count: int
    passed_count: int
    pass_rate: float
    trace_sufficiency_rate: float
    argument_constraint_pass_rate: float
    missing_required_argument_rate: float
    scope_overreach_count: int
    tool_result_availability_rate: float
    cited_source_coverage: float
    unknown_citation_count: int
    required_claim_support_rate: float
    unsupported_claim_count: int
    forbidden_claim_violation_count: int
    results: tuple[ToolEvidenceCaseResult, ...]


__all__ = [
    "ArgumentConstraintResult",
    "ConstraintOperator",
    "EvidenceClaim",
    "EvidenceClaimResult",
    "ForbiddenAnswerClaim",
    "ScopeViolation",
    "ToolArgConstraint",
    "ToolArgumentExpectation",
    "ToolArgumentExpectationResult",
    "ToolCallEvidence",
    "ToolEvidenceCaseResult",
    "ToolEvidenceEvalCase",
    "ToolEvidenceObservation",
    "ToolEvidenceSuiteResult",
    "ToolEvidenceTraceIssue",
    "ToolScopeRule",
]
