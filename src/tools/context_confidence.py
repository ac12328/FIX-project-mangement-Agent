"""Deterministic context-confidence policy for sensitive tool execution."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping


@dataclass(frozen=True)
class ContextConfidenceDecision:
    """Trusted policy result for one tool authorization attempt."""

    allowed: bool
    confidence: float | None = None
    minimum: float | None = None
    missing_flags: tuple[str, ...] = ()
    reason: str = "not_required"
    bypassed: bool = False


class ContextConfidenceError(PermissionError):
    """Raised when a sensitive tool is attempted with insufficient context."""

    code = "context_confidence_too_low"

    def __init__(self, tool_name: str, decision: ContextConfidenceDecision) -> None:
        self.tool_name = tool_name
        self.decision = decision
        flags = ", ".join(decision.missing_flags) or "unspecified_context_gaps"
        super().__init__(
            f"Tool '{tool_name}' requires context confidence >= "
            f"{decision.minimum:.2f}; received {decision.confidence:.2f}. "
            f"Missing context: {flags}. Read current state or ask the operator "
            "for clarification before retrying."
        )

    def as_result(self) -> dict[str, Any]:
        return {
            "ok": False,
            "error": str(self),
            "error_code": self.code,
            "retryable": False,
            "context_confidence": self.decision.confidence,
            "minimum_context_confidence": self.decision.minimum,
            "missing_context_flags": list(self.decision.missing_flags),
            "side_effect_status": "not_started",
        }


def _coerce_confidence(value: Any) -> float | None:
    if isinstance(value, bool) or value is None:
        return None
    try:
        confidence = float(value)
    except (TypeError, ValueError):
        return None
    if confidence < 0.0 or confidence > 1.0:
        return None
    return confidence


def _coerce_missing_flags(value: Any) -> tuple[str, ...]:
    if isinstance(value, str):
        values = (value,)
    elif isinstance(value, (list, tuple, set, frozenset)):
        values = tuple(str(item) for item in value)
    else:
        return ()
    return tuple(sorted({item.strip() for item in values if item.strip()}))


def evaluate_context_confidence(
    *,
    minimum: float | None,
    execution_context: Mapping[str, Any],
) -> ContextConfidenceDecision:
    """Evaluate trusted context metadata without inspecting model arguments.

    Missing metadata is allowed for backward-compatible non-agent callers. Production
    loops attach ContextFrame confidence in LoopConfig.__post_init__.
    """
    if minimum is None:
        return ContextConfidenceDecision(allowed=True)

    confidence = _coerce_confidence(execution_context.get("context_confidence"))
    missing_flags = _coerce_missing_flags(
        execution_context.get("missing_context_flags")
    )
    if confidence is None:
        return ContextConfidenceDecision(
            allowed=True,
            minimum=minimum,
            missing_flags=missing_flags,
            reason="confidence_not_supplied",
        )

    bypassed = bool(
        execution_context.get("explicit_operator_instruction")
        or execution_context.get("deterministic_authorization")
        or execution_context.get("approved_action_id")
    )
    if bypassed:
        return ContextConfidenceDecision(
            allowed=True,
            confidence=confidence,
            minimum=minimum,
            missing_flags=missing_flags,
            reason="trusted_authorization_bypass",
            bypassed=True,
        )

    if confidence >= minimum:
        return ContextConfidenceDecision(
            allowed=True,
            confidence=confidence,
            minimum=minimum,
            missing_flags=missing_flags,
            reason="threshold_met",
        )

    return ContextConfidenceDecision(
        allowed=False,
        confidence=confidence,
        minimum=minimum,
        missing_flags=missing_flags,
        reason="below_threshold",
    )
