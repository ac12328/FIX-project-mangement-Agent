"""Strict schemas and validation for Policy-v4 offline automated review."""

from __future__ import annotations

import re
from typing import Any, Iterable

from src.assistant.evals.grounding_review import (
    ISSUE_CODES,
    RUBRIC_DIMENSIONS,
    score_answer,
)

_CITATION_RE = re.compile(r"\[\[source:([A-Za-z0-9][A-Za-z0-9_.:-]*)\]\]")
_LOOSE_CITATION_RE = re.compile(
    r"\[\[\s*source\s*:\s*([A-Za-z0-9][A-Za-z0-9_.:-]*)\s*\]\]",
    re.IGNORECASE,
)
_DIMENSION_LIMITS = {item.key: float(item.weight) for item in RUBRIC_DIMENSIONS}
_DIMENSIONS = tuple(_DIMENSION_LIMITS)


class ReferenceOutputShapeError(ValueError):
    """Reference output does not match the required structure."""


class ReferenceDeclaredSourceError(ValueError):
    """Reference output declares a source outside frozen evidence."""


class ReferenceMalformedCitationError(ValueError):
    """Reference answer contains a malformed citation marker."""


class ReferenceUnknownCitationError(ValueError):
    """Reference answer cites a source outside frozen evidence."""


class ReferenceMissingCitationError(ValueError):
    """Reference answer omitted citations and declared no usable source IDs."""


class ReferenceMissingDeclaredSourceError(ValueError):
    """Reference output omitted source declarations despite available evidence."""


class ReferenceChunkShapeError(ValueError):
    """Reference extraction chunk does not match the required structure."""


class ReferenceChunkSourceError(ValueError):
    """Reference extraction chunk declares a source outside its evidence chunk."""


class JudgeSideShapeError(ValueError):
    """A judge side does not match the locked rubric structure."""


class JudgeIssueCodeError(ValueError):
    """A judge side returned an issue code outside the locked vocabulary."""


class JudgeScoreError(ValueError):
    """A judge side returned a score outside the locked rubric range."""


class JudgePreferenceError(ValueError):
    """The judge preference is not A, B, or TIE."""


def _source_item_schema(allowed_source_ids: Iterable[str]) -> dict[str, Any]:
    allowed = sorted({str(item).strip() for item in allowed_source_ids if str(item).strip()})
    schema: dict[str, Any] = {"type": "string"}
    if allowed:
        schema["enum"] = allowed
    return schema


def reference_schema(allowed_source_ids: Iterable[str]) -> dict[str, Any]:
    allowed = sorted({str(item).strip() for item in allowed_source_ids if str(item).strip()})
    source_ids_schema: dict[str, Any] = {
        "type": "array",
        "items": _source_item_schema(allowed),
        "uniqueItems": True,
    }
    if allowed:
        source_ids_schema["minItems"] = 1
    return {
        "type": "object",
        "properties": {
            "reference_answer": {
                "type": "string",
                "description": (
                    "Answer the question and cite factual claims using exact "
                    "[[source:source_id]] markers from allowed_source_ids."
                ),
            },
            "required_facts": {"type": "array", "items": {"type": "string"}},
            "source_ids": source_ids_schema,
        },
        "required": ["reference_answer", "required_facts", "source_ids"],
        "additionalProperties": False,
    }


def reference_chunk_schema(allowed_source_ids: Iterable[str]) -> dict[str, Any]:
    return {
        "type": "object",
        "properties": {
            "relevant_facts": {"type": "array", "items": {"type": "string"}},
            "source_ids": {"type": "array", "items": _source_item_schema(allowed_source_ids)},
            "contradictions": {"type": "array", "items": {"type": "string"}},
        },
        "required": ["relevant_facts", "source_ids", "contradictions"],
        "additionalProperties": False,
    }


def _side_schema() -> dict[str, Any]:
    return {
        "type": "object",
        "properties": {
            **{
                key: {"type": "number", "minimum": 0.0, "maximum": maximum}
                for key, maximum in _DIMENSION_LIMITS.items()
            },
            "issue_codes": {
                "type": "array",
                "items": {"type": "string", "enum": list(ISSUE_CODES)},
            },
            "summary": {"type": "string"},
        },
        "required": [*_DIMENSIONS, "issue_codes", "summary"],
        "additionalProperties": False,
    }


JUDGE_SCHEMA = {
    "type": "object",
    "properties": {
        "answer_a": _side_schema(),
        "answer_b": _side_schema(),
        "preferred_answer": {"type": "string", "enum": ["A", "B", "TIE"]},
        "confidence": {"type": "number", "minimum": 0.0, "maximum": 1.0},
        "comparison_summary": {"type": "string"},
    },
    "required": ["answer_a", "answer_b", "preferred_answer", "confidence", "comparison_summary"],
    "additionalProperties": False,
}

CHUNK_JUDGE_SCHEMA = {
    "type": "object",
    "properties": {
        "answer_a_findings": {"type": "array", "items": {"type": "string"}},
        "answer_b_findings": {"type": "array", "items": {"type": "string"}},
        "answer_a_issue_codes": {
            "type": "array",
            "items": {"type": "string", "enum": list(ISSUE_CODES)},
        },
        "answer_b_issue_codes": {
            "type": "array",
            "items": {"type": "string", "enum": list(ISSUE_CODES)},
        },
        "source_ids_checked": {"type": "array", "items": {"type": "string"}},
    },
    "required": [
        "answer_a_findings",
        "answer_b_findings",
        "answer_a_issue_codes",
        "answer_b_issue_codes",
        "source_ids_checked",
    ],
    "additionalProperties": False,
}


def _canonicalize_known_citations(answer: str, evidence_ids: set[str]) -> str:
    def replace(match: re.Match[str]) -> str:
        source_id = match.group(1)
        return f"[[source:{source_id}]]" if source_id in evidence_ids else match.group(0)

    return _LOOSE_CITATION_RE.sub(replace, answer)


def validate_reference_output(
    result: dict[str, Any], evidence_ids: set[str],
) -> tuple[str, list[str]]:
    answer = str(result.get("reference_answer") or "").strip()
    facts = result.get("required_facts")
    declared = result.get("source_ids")
    if not answer or not isinstance(facts, list) or not isinstance(declared, list):
        raise ReferenceOutputShapeError
    declared_ids = {str(item).strip() for item in declared if str(item).strip()}
    if not declared_ids.issubset(evidence_ids):
        raise ReferenceDeclaredSourceError
    if evidence_ids and not declared_ids:
        raise ReferenceMissingDeclaredSourceError
    answer = _canonicalize_known_citations(answer, evidence_ids)
    citations = set(_CITATION_RE.findall(answer))
    if "[[source:" in _CITATION_RE.sub("", answer):
        raise ReferenceMalformedCitationError
    if not citations.issubset(evidence_ids):
        raise ReferenceUnknownCitationError
    if evidence_ids and not citations:
        if not declared_ids:
            raise ReferenceMissingCitationError
        citation_footer = " ".join(
            f"[[source:{source_id}]]" for source_id in sorted(declared_ids)
        )
        answer = f"{answer.rstrip()}\n\nSources: {citation_footer}"
    return answer, [str(item) for item in facts]


def validate_reference_chunk_output(
    result: dict[str, Any], chunk_ids: set[str],
) -> None:
    if (
        not isinstance(result.get("relevant_facts"), list)
        or not isinstance(result.get("source_ids"), list)
        or not isinstance(result.get("contradictions"), list)
    ):
        raise ReferenceChunkShapeError
    declared_ids = {
        str(item).strip() for item in result["source_ids"] if str(item).strip()
    }
    if not declared_ids.issubset(chunk_ids):
        raise ReferenceChunkSourceError


def normalize_side(
    value: Any, *, forced_issues: Iterable[str] = (),
) -> tuple[dict[str, float], list[str], float]:
    if not isinstance(value, dict) or not isinstance(value.get("issue_codes"), list):
        raise JudgeSideShapeError
    issues = list(dict.fromkeys(
        [
            *(str(item).strip() for item in value["issue_codes"]),
            *(str(item).strip() for item in forced_issues),
        ]
    ))
    if set(issues) - set(ISSUE_CODES):
        raise JudgeIssueCodeError
    dimensions = {key: value.get(key) for key in _DIMENSIONS}
    try:
        scored = score_answer(dimensions, issues)
    except ValueError as exc:
        raise JudgeScoreError from exc
    return dict(scored.dimensions), list(scored.issue_codes), float(scored.total)


def validate_preference(value: Any) -> str:
    preferred = str(value or "").upper()
    if preferred not in {"A", "B", "TIE"}:
        raise JudgePreferenceError
    return preferred


__all__ = [
    "CHUNK_JUDGE_SCHEMA",
    "JUDGE_SCHEMA",
    "normalize_side",
    "reference_chunk_schema",
    "reference_schema",
    "validate_preference",
    "validate_reference_chunk_output",
    "validate_reference_output",
]
