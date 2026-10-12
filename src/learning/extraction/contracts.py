"""Typed proposal contracts for governed learning extraction."""
from __future__ import annotations

import json
import math
from datetime import datetime
from enum import Enum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class CandidateTarget(str, Enum):
    SEMANTIC_FACT = "semantic_fact"
    PREFERENCE = "preference"
    PROCEDURE = "procedure"
    WIKI_PUBLICATION = "wiki_publication"


class ScopeType(str, Enum):
    CONTACT = "contact"
    PROJECT = "project"
    COUNTERPARTY = "counterparty"
    OPERATOR = "operator"
    MARKET = "market"
    ORGANIZATION = "organization"
    GLOBAL = "global"


def _validate_bounded_json(value: Any, *, depth: int = 0) -> None:
    if not isinstance(value, (str, int, float, bool, list, dict, type(None))):
        raise ValueError("proposed value must be JSON-compatible")
    if depth > 3:
        raise ValueError("proposed value exceeds maximum depth")
    if isinstance(value, float) and not math.isfinite(value):
        raise ValueError("proposed value numbers must be finite")
    if isinstance(value, str) and len(value) > 2000:
        raise ValueError("proposed value string is too large")
    if isinstance(value, list):
        if len(value) > 20:
            raise ValueError("proposed value list is too large")
        for item in value:
            _validate_bounded_json(item, depth=depth + 1)
    elif isinstance(value, dict):
        if len(value) > 20:
            raise ValueError("proposed value object is too large")
        for key, item in value.items():
            if len(key) > 100:
                raise ValueError("proposed value key is too large")
            _validate_bounded_json(item, depth=depth + 1)


class MemoryCandidateProposal(BaseModel):
    """LLM suggestions only; authoritative identity and scope are system-derived."""

    model_config = ConfigDict(extra="forbid")

    target_type: CandidateTarget
    suggested_subject_type: str | None = Field(default=None, max_length=80)
    suggested_subject_id: str | None = Field(default=None, max_length=200)
    predicate: str = Field(max_length=160)
    proposed_value: Any
    suggested_scope_type: ScopeType | None = None
    suggested_scope_id: str | None = Field(default=None, max_length=200)
    evidence_refs: list[str] = Field(min_length=1, max_length=1)
    confidence: float = Field(ge=0.0, le=1.0)
    suggested_expiry: datetime | None = None
    reasoning_summary: str = Field(min_length=1, max_length=1000)
    risk_signals: list[str] = Field(default_factory=list, max_length=20)

    @field_validator("predicate")
    @classmethod
    def _non_empty(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("candidate predicate is required")
        return value.strip()

    @model_validator(mode="after")
    def _bounded_output(self):
        _validate_bounded_json(self.proposed_value)
        encoded = json.dumps(self.proposed_value, ensure_ascii=False, sort_keys=True, allow_nan=False)
        if len(encoded.encode("utf-8")) > 8192:
            raise ValueError("proposed value exceeds serialized byte limit")
        return self


class ExtractionResult(BaseModel):
    proposal: MemoryCandidateProposal
    subject_type: str
    subject_id: str
    scope_type: ScopeType
    scope_id: str
    extractor_version: str
    model_version: str
    prompt_version: str
    schema_version: str
    input_policy_version: str
