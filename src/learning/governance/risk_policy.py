"""Deterministic finding severity and aggregate risk classification."""
from __future__ import annotations

_SEVERITY = {
    "info": 0,
    "low": 10,
    "medium": 30,
    "high": 60,
    "critical": 100,
}
_CRITICAL = {
    "contains_secret", "possible_prompt_injection", "unsupported_policy_change",
    "exfiltration_instruction", "untrusted_instruction_as_procedure",
}
_HIGH = {"contains_pii", "sensitivity_not_allowed", "scope_too_broad", "cross_project_scope"}
_MEDIUM = {
    "missing_evidence", "unauthorized_evidence", "insufficient_independent_sources",
    "missing_expiry", "single_incident_procedure", "canonical_subject_mismatch",
    "invalid_expiry", "expired_before_validation",
}


def severity_for(code: str) -> str:
    if code in _CRITICAL:
        return "critical"
    if code in _HIGH:
        return "high"
    if code in _MEDIUM:
        return "medium"
    return "low"


def classify(codes: set[str], *, target_type: str, scope_type: str) -> tuple[str, int]:
    levels = [severity_for(code) for code in codes]
    if target_type in {"procedure", "wiki_publication"} or scope_type in {"market", "global"}:
        levels.append("high")
    score = max((_SEVERITY[level] for level in levels), default=10)
    if score >= 100:
        return "critical", score
    if score >= 60:
        return "high", score
    if score >= 30:
        return "medium", score
    return "low", score
