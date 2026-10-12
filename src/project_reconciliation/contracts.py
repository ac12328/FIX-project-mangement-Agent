"""Typed contracts for durable email-to-project reconciliation."""

from __future__ import annotations

from enum import Enum
from typing import Any

from pydantic import BaseModel, Field


class ReconciliationMode(str, Enum):
    PREVIEW = "preview"
    COMMIT = "commit"


class EvidenceRecord(BaseModel):
    source_ref: str
    source_table: str
    source_id: str
    project_match: str
    event_at: str
    subject: str = ""
    sender: str = ""
    content: str
    content_hash: str


class EvidenceCitation(BaseModel):
    source_ref: str
    quote: str
    event_at: str
    reason: str


class FrozenEvidenceBundle(BaseModel):
    project_id: str
    records: list[EvidenceRecord] = Field(default_factory=list)
    bundle_hash: str
    frozen_at: str


class ReconciliationProposal(BaseModel):
    project_id: str
    project_exists: bool
    base_version: str | None = None
    fields: dict[str, Any] = Field(default_factory=dict)
    inferred_milestone: str | None = None
    bundle_hash: str
    evidence_refs: list[str] = Field(default_factory=list)
    citations: list[EvidenceCitation] = Field(default_factory=list)
    rationale: list[str] = Field(default_factory=list)
    confidence: float = 0.0
    assessment: dict[str, Any] | None = None
    replay_steps: list[dict[str, Any]] = Field(default_factory=list)


class ReconciliationResult(BaseModel):
    ok: bool
    status: str
    outcome: str
    committed: bool = False
    project_id: str
    operation_id: str
    project_created: bool = False
    applied_fields: dict[str, Any] = Field(default_factory=dict)
    proposed_fields: dict[str, Any] = Field(default_factory=dict)
    current_project: dict[str, Any] | None = None
    bundle_hash: str | None = None
    evidence_refs: list[str] = Field(default_factory=list)
    proposed_update_id: str | None = None
    error_code: str | None = None
    error: str | None = None
    text: str = ""
