"""Typed WikiPage v2 governance contracts."""
from __future__ import annotations

from enum import Enum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class WikiScope(str, Enum):
    GLOBAL = "global"
    MARKET = "market"
    COUNTERPARTY = "counterparty"
    PROJECT = "project"
    EXAMPLE = "example"
    INTERNAL_RUNBOOK = "internal_runbook"


class WikiVisibility(str, Enum):
    INTERNAL_ONLY = "internal_only"
    EXTERNAL_SAFE = "external_safe"
    RESTRICTED = "restricted"


class VerificationStatus(str, Enum):
    UNREVIEWED = "unreviewed"
    REVIEWED = "reviewed"
    OPERATOR_VERIFIED = "operator_verified"
    REJECTED = "rejected"


class ConflictState(str, Enum):
    NONE = "none"
    UNRESOLVED = "unresolved"
    RESOLVED = "resolved"


class ConfidenceMethod(str, Enum):
    EXTRACTED = "extracted"
    INFERRED = "inferred"
    CORROBORATED = "corroborated"
    OPERATOR_VERIFIED = "operator_verified"


class WikiConfidence(BaseModel):
    model_config = ConfigDict(extra="forbid")

    level: Literal["high", "medium", "low"]
    method: ConfidenceMethod = ConfidenceMethod.EXTRACTED


class WikiSource(BaseModel):
    model_config = ConfigDict(extra="forbid")

    source_type: Literal["email", "thread", "document", "operator", "evidence", "rule", "code"]
    source_id: str = Field(min_length=1)
    project_id: str | None = None
    observed_at: str | None = None
    authority: str | None = None
    locator: str | None = None
    excerpt_hash: str | None = None

    def legacy_locator(self) -> str:
        """Return a compact locator for v1-compatible callers."""
        prefix = "doc" if self.source_type == "document" else self.source_type
        return f"{prefix}:{self.source_id}"


class WikiGovernance(BaseModel):
    """Validated v2 governance metadata independent of Markdown body."""

    model_config = ConfigDict(extra="forbid")

    schema_version: Literal[2] = 2
    page_id: str = Field(pattern=r"^wiki_[a-f0-9]{16}$")
    scope: WikiScope
    visibility: WikiVisibility = WikiVisibility.INTERNAL_ONLY
    external_grounding: bool = False
    project_ids: list[str] = Field(default_factory=list)
    markets: list[str] = Field(default_factory=list)
    counterparties: list[str] = Field(default_factory=list)
    environments: list[str] = Field(default_factory=list)
    effective_from: str | None = None
    effective_until: str | None = None
    reviewed_at: str | None = None
    reviewed_by: str | None = None
    owner: str | None = None
    verification_status: VerificationStatus = VerificationStatus.UNREVIEWED
    canonical_path: str
    aliases: list[str] = Field(default_factory=list)
    superseded_by: str | None = None
    archive_reason: str | None = None
    conflict_state: ConflictState = ConflictState.NONE
    revision: int = Field(default=1, ge=1)
    content_hash: str | None = None
    confidence: WikiConfidence
    provenance: list[WikiSource] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_scope_requirements(self) -> "WikiGovernance":
        if self.scope == WikiScope.PROJECT and not self.project_ids:
            raise ValueError("project scope requires project_ids")
        if self.scope == WikiScope.MARKET and not self.markets:
            raise ValueError("market scope requires markets")
        if self.scope == WikiScope.COUNTERPARTY and not self.counterparties:
            raise ValueError("counterparty scope requires counterparties")
        if self.scope == WikiScope.EXAMPLE and not (
            self.project_ids or self.counterparties or self.markets
        ):
            raise ValueError("example scope requires source applicability")
        if self.external_grounding and self.visibility != WikiVisibility.EXTERNAL_SAFE:
            raise ValueError("external_grounding requires external_safe visibility")
        return self
