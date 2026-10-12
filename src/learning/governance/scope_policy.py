"""Scope, canonical subject, evidence authorization, and source-count policy."""
from __future__ import annotations

from typing import Any

_SCOPE_RANK = {"contact": 0, "project": 1, "counterparty": 2, "operator": 3, "market": 4, "organization": 5, "global": 6}


def scope_codes(candidate: Any, observations: list[Any], *, minimum_sources: int) -> set[str]:
    codes: set[str] = set()
    expected_subject_id = candidate.scope_id
    if candidate.scope_type != candidate.subject_type or candidate.subject_id != expected_subject_id:
        codes.add("canonical_subject_mismatch")
    source_projects = {item.project_id for item in observations if item.project_id}
    source_contacts = {item.contact_id for item in observations if item.contact_id}
    source_counterparties = {item.counterparty_id for item in observations if item.counterparty_id}
    source_operators = {item.operator_id for item in observations if item.operator_id}
    independent = len({(item.source_type.value, item.source_id) for item in observations})
    if independent < minimum_sources:
        codes.add("insufficient_independent_sources")
    if candidate.scope_type == "project" and candidate.scope_id not in source_projects:
        codes.add("unauthorized_evidence")
    elif candidate.scope_type == "contact" and candidate.scope_id not in source_contacts:
        codes.add("unauthorized_evidence")
    elif candidate.scope_type == "counterparty" and candidate.scope_id not in source_counterparties:
        codes.add("unauthorized_evidence")
    elif candidate.scope_type == "operator" and candidate.scope_id not in source_operators:
        codes.add("unauthorized_evidence")
    source_max_rank = max((_source_rank(item) for item in observations), default=-1)
    candidate_rank = _SCOPE_RANK.get(candidate.scope_type, 99)
    if candidate_rank > source_max_rank:
        codes.add("scope_too_broad")
    if candidate.scope_type in {"market", "global"} and len(source_projects) < minimum_sources:
        codes.add("cross_project_scope")
    return codes


def _source_rank(observation: Any) -> int:
    if observation.contact_id:
        return _SCOPE_RANK["contact"]
    if observation.project_id:
        return _SCOPE_RANK["project"]
    if observation.counterparty_id:
        return _SCOPE_RANK["counterparty"]
    return _SCOPE_RANK["operator"]
