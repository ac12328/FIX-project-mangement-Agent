"""Typed, versioned policy for deterministic candidate governance."""
from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class GovernancePolicy(BaseModel):
    """Policy values are serialized into validation hashes and never model-controlled."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    policy_version: str = "learning-governance-policy-v1"
    validator_version: str = "deterministic-governance-validator-v1"
    max_revisions: int = Field(default=2, ge=0, le=2)
    expiry_required_targets: frozenset[str] = frozenset({
        "preference", "procedure", "wiki_publication",
    })
    expiry_required_scopes: frozenset[str] = frozenset({"market", "global"})
    review_required_targets: frozenset[str] = frozenset({"procedure", "wiki_publication"})
    evaluation_required_targets: frozenset[str] = frozenset({"procedure", "wiki_publication"})
    review_required_scopes: frozenset[str] = frozenset({"market", "global"})
    evaluation_required_scopes: frozenset[str] = frozenset({"market", "global"})
    min_sources_by_scope: dict[str, int] = Field(default_factory=lambda: {
        "contact": 1,
        "project": 1,
        "counterparty": 2,
        "operator": 1,
        "market": 2,
        "global": 3,
    })
    allowed_sensitivity_by_target: dict[str, frozenset[str]] = Field(default_factory=lambda: {
        "semantic_fact": frozenset({"public", "internal", "confidential"}),
        "preference": frozenset({"public", "internal"}),
        "procedure": frozenset({"public", "internal"}),
        "wiki_publication": frozenset({"public", "internal"}),
    })
    repairable_findings: frozenset[str] = frozenset({
        "invalid_schema", "missing_evidence", "unauthorized_evidence", "scope_too_broad",
        "insufficient_independent_sources", "cross_project_scope", "missing_expiry",
        "single_incident_procedure", "canonical_subject_mismatch",
    })
    quarantine_findings: frozenset[str] = frozenset({
        "contains_secret", "contains_pii", "sensitivity_not_allowed",
        "possible_prompt_injection", "exfiltration_instruction",
        "untrusted_instruction_as_procedure",
    })
    reject_findings: frozenset[str] = frozenset({"unsupported_policy_change"})
    terminal_after_repairs: Literal["needs_review", "quarantined", "rejected"] = "needs_review"
