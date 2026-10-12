"""Shared tool-argument constraint contract and deterministic matching semantics.

Kept outside evaluation packages so runtime trace replay does not import
versioned research/evaluation code.
"""

from __future__ import annotations

from enum import Enum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class EvalModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ConstraintOperator(str, Enum):
    EQ = "eq"
    GTE = "gte"
    LTE = "lte"
    CONTAINS_EXACT = "contains_exact"
    ABSENT = "absent"
    PRESENT = "present"


class ToolArgConstraint(EvalModel):
    path: str = Field(min_length=1)
    operator: ConstraintOperator
    value: Any = None
    critical: bool = True
    description: str = ""


_MISSING = object()


def _resolve_path(value: Any, path: str) -> Any:
    current = value
    for segment in (item for item in path.split(".") if item):
        if isinstance(current, dict) and segment in current:
            current = current[segment]
        else:
            return _MISSING
    return current


def constraint_passes(arguments: dict[str, Any], constraint: ToolArgConstraint) -> bool:
    """Evaluate a constraint against raw arguments without schema defaults."""
    actual = _resolve_path(arguments, constraint.path)
    operator = constraint.operator
    expected = constraint.value
    if operator == ConstraintOperator.ABSENT:
        return actual is _MISSING
    if operator == ConstraintOperator.PRESENT:
        return actual is not _MISSING
    if actual is _MISSING:
        return False
    if operator == ConstraintOperator.EQ:
        return actual == expected
    if operator == ConstraintOperator.GTE:
        try:
            return actual >= expected
        except TypeError:
            return False
    if operator == ConstraintOperator.LTE:
        try:
            return actual <= expected
        except TypeError:
            return False
    if operator == ConstraintOperator.CONTAINS_EXACT:
        if not isinstance(actual, (list, tuple, set)):
            return False
        required = expected if isinstance(expected, list) else [expected]
        return all(item in actual for item in required)
    return False
