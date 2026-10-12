"""Typed contracts for governed Wiki publication."""
from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class PublicationPlan(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    publication_id: str
    fact_id: str
    fact_version: int
    decision_id: str
    operation: Literal["upsert", "revoke"]
    canonical_path: str
    title: str
    page_type: str
    body: str
    scope: Literal["global", "project", "market", "counterparty"]
    visibility: Literal["internal_only", "external_safe", "restricted"]
    project_ids: list[str] = Field(default_factory=list)
    markets: list[str] = Field(default_factory=list)
    counterparties: list[str] = Field(default_factory=list)
    source_watermark: str
    evidence_refs: list[str]
    planned_content_hash: str
    ownership: dict
    proposed_at: str

    @model_validator(mode="after")
    def validate_scope(self) -> "PublicationPlan":
        required = {
            "project": self.project_ids,
            "market": self.markets,
            "counterparty": self.counterparties,
        }
        if self.scope in required and not required[self.scope]:
            raise ValueError(f"{self.scope} scope requires an identifier")
        if self.scope == "project" and self.visibility != "restricted":
            raise ValueError("project scope must be restricted")
        return self


class PublicationRecord(BaseModel):
    model_config = ConfigDict(frozen=True)
    publication_id: str
    fact_id: str
    status: str
    canonical_path: str
    operation: str = "upsert"
    page_revision: int = Field(ge=0)
    page_hash: str | None = None
    repair_count: int = Field(ge=0, le=2)
    last_error: str | None = None


class MaintenanceResult(BaseModel):
    model_config = ConfigDict(frozen=True)
    discovered: int = 0
    proposed: int = 0
    completed: int = 0
    failed: int = 0
    manual_review: int = 0
    dead_letter: int = 0
