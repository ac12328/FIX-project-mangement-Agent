"""Typed evaluation fixtures, private loading, and fail-closed manifest validation."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from src.learning.evaluation.assertions import ASSERTION_CODES


class EvaluationCase(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    case_id: str
    suite_name: str
    suite_version: str
    project_id: str | None = None
    input_payload: dict[str, Any]
    expected_keywords: list[str]
    required_assertions: list[str]
    expected_recipient: str | None = None
    expected_tone: str | None = None
    expected_tool: str | None = None
    expected_contact_id: str | None = None
    expected_evidence_ref: str | None = None
    forbidden_values: list[str] = Field(default_factory=list)


def validate_case_manifest(cases: list[EvaluationCase]) -> None:
    from src.learning.evaluation.suites import SUITE_DEFINITIONS

    ids = [case.case_id for case in cases]
    if len(ids) != len(set(ids)):
        raise ValueError("duplicate case id")
    for case in cases:
        definition = SUITE_DEFINITIONS.get(case.suite_name)
        if definition is None:
            raise ValueError("unknown suite")
        if case.suite_version != definition.version:
            raise ValueError("wrong suite version")
        unknown = set(case.required_assertions) - ASSERTION_CODES
        if unknown:
            raise ValueError("unknown assertion")
        if not case.expected_keywords:
            raise ValueError("empty expected quality")
        if not set(definition.required_assertions) <= set(case.required_assertions):
            raise ValueError("missing required assertion")
        for field in definition.required_fields:
            if not getattr(case, field):
                raise ValueError(f"missing required fixture field: {field}")


def load_private_cases(path: Path) -> list[EvaluationCase]:
    cases = []
    for file in sorted(path.glob("*.json")):
        payload = json.loads(file.read_text(encoding="utf-8"))
        rows = payload if isinstance(payload, list) else [payload]
        cases.extend(EvaluationCase.model_validate(row) for row in rows)
    validate_case_manifest(cases)
    return cases
