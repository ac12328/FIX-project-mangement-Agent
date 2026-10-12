"""Typed contracts for deterministic semantic tool-selection evaluation."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

FUNNEL_KEYS = (
    "selected",
    "authorized",
    "blocked",
    "executed",
    "succeeded",
    "failed",
    "denied",
    "duplicate_selection",
)


class ToolSelectionEvalModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ToolSelectionEvalCase(ToolSelectionEvalModel):
    """Frozen semantic oracle for one tool-selection decision."""

    schema_version: Literal[1] = 1
    case_id: str = Field(pattern=r"^[a-z0-9][a-z0-9_-]{2,79}$")
    user_request: str = Field(min_length=1)
    surface: str = Field(min_length=1)
    role: str = Field(min_length=1)
    intent: str = Field(min_length=1)
    available_tools: tuple[str, ...] = ()
    expected_tools: tuple[str, ...] = ()
    forbidden_tools: tuple[str, ...] = ()
    tags: tuple[str, ...] = ()

    @field_validator(
        "available_tools",
        "expected_tools",
        "forbidden_tools",
        "tags",
        mode="before",
    )
    @classmethod
    def normalize_unique_values(cls, value: Any) -> tuple[str, ...]:
        if value is None:
            return ()
        if isinstance(value, str):
            value = (value,)
        normalized: list[str] = []
        seen: set[str] = set()
        for item in value:
            text = str(item).strip()
            if not text:
                raise ValueError("tool and tag values must not be blank")
            if text not in seen:
                seen.add(text)
                normalized.append(text)
        return tuple(normalized)

    @model_validator(mode="after")
    def validate_tool_sets(self) -> "ToolSelectionEvalCase":
        available = set(self.available_tools)
        expected = set(self.expected_tools)
        forbidden = set(self.forbidden_tools)
        missing_expected = expected - available
        missing_forbidden = forbidden - available
        overlap = expected & forbidden
        if missing_expected:
            raise ValueError(
                f"expected tools are not available: {sorted(missing_expected)}"
            )
        if missing_forbidden:
            raise ValueError(
                f"forbidden tools are not available: {sorted(missing_forbidden)}"
            )
        if overlap:
            raise ValueError(
                f"tools cannot be both expected and forbidden: {sorted(overlap)}"
            )
        return self


class ToolSelectionFunnel(ToolSelectionEvalModel):
    selected: int = Field(default=0, ge=0)
    authorized: int = Field(default=0, ge=0)
    blocked: int = Field(default=0, ge=0)
    executed: int = Field(default=0, ge=0)
    succeeded: int = Field(default=0, ge=0)
    failed: int = Field(default=0, ge=0)
    denied: int = Field(default=0, ge=0)
    duplicate_selection: int = Field(default=0, ge=0)


class ToolSelectionRound(ToolSelectionEvalModel):
    round_index: int = Field(ge=1)
    event_id: str | None = None
    sequence: int | None = None
    active_tools: tuple[str, ...] = ()
    selected_tools: tuple[str, ...] = ()
    observable_outcome: str | None = None
    funnel: ToolSelectionFunnel = Field(default_factory=ToolSelectionFunnel)


class ToolSelectionTraceIssue(ToolSelectionEvalModel):
    code: str
    severity: Literal["warning", "error"]
    message: str
    event_id: str | None = None


class ToolSelectionObservation(ToolSelectionEvalModel):
    run_id: str | None = None
    trace_id: str | None = None
    surface: str | None = None
    role: str | None = None
    intent: str | None = None
    available_tools: tuple[str, ...] = ()
    rounds: tuple[ToolSelectionRound, ...] = ()
    issues: tuple[ToolSelectionTraceIssue, ...] = ()


class ToolSelectionCaseResult(ToolSelectionEvalModel):
    case_id: str
    run_id: str | None = None
    selected_tools: tuple[str, ...]
    selected_tool_counts: dict[str, int]
    true_positive_tools: tuple[str, ...]
    missing_required_tools: tuple[str, ...]
    unexpected_tools: tuple[str, ...]
    forbidden_tools_selected: tuple[str, ...]
    unexposed_required_tools: tuple[str, ...]
    available_tool_drift: tuple[str, ...]
    selected_count: int
    authorized_count: int
    authorization_denial_count: int
    pre_execution_block_count: int
    executed_count: int
    duplicate_selection_count: int
    unnecessary_selection_count: int
    forbidden_selection_count: int
    precision: float
    recall: float
    f1: float
    authorization_block_rate: float
    exact_match: bool
    trace_valid: bool
    context_match: bool
    passed: bool
    findings: tuple[str, ...] = ()
    observation: ToolSelectionObservation


class ToolSelectionSuiteResult(ToolSelectionEvalModel):
    schema_version: Literal[1] = 1
    case_count: int
    passed_count: int
    pass_rate: float
    exact_match_rate: float
    macro_precision: float
    macro_recall: float
    macro_f1: float
    required_tool_miss_rate: float
    unnecessary_selection_rate: float
    forbidden_selection_rate: float
    duplicate_selection_rate: float
    authorization_block_rate: float
    results: tuple[ToolSelectionCaseResult, ...]


__all__ = [
    "FUNNEL_KEYS",
    "ToolSelectionCaseResult",
    "ToolSelectionEvalCase",
    "ToolSelectionFunnel",
    "ToolSelectionObservation",
    "ToolSelectionRound",
    "ToolSelectionSuiteResult",
    "ToolSelectionTraceIssue",
]
