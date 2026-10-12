"""Typed contracts for governed legacy learning backfill."""
from __future__ import annotations

from collections import Counter
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


LegacyStore = Literal[
    "agent_notes",
    "operator_memory",
    "learned_rules",
    "response_pattern_memory",
]


class BackfillRowResult(BaseModel):
    """Deterministic outcome for one legacy row or response field."""

    model_config = ConfigDict(frozen=True)

    store: LegacyStore
    legacy_row_id: str
    source_id: str
    disposition: Literal[
        "created",
        "existing",
        "dry_run",
        "skipped_empty",
        "quarantined",
        "error",
    ]
    observation_id: str | None = None
    candidate_id: str | None = None
    candidate_version: int | None = None
    governance_disposition: str | None = None
    conflict_match: str | None = None
    conflict_id: str | None = None
    detail: str | None = None


class BackfillSummary(BaseModel):
    """JSON-safe migration summary derived without migration tracking tables."""

    model_config = ConfigDict(frozen=True)

    dry_run: bool
    operator_id: str
    results: list[BackfillRowResult] = Field(default_factory=list)

    @property
    def totals(self) -> dict[str, int]:
        return dict(sorted(Counter(item.disposition for item in self.results).items()))

    @property
    def by_store(self) -> dict[str, int]:
        return dict(sorted(Counter(item.store for item in self.results).items()))

    def as_json_dict(self) -> dict:
        value = self.model_dump(mode="json")
        value["totals"] = self.totals
        value["by_store"] = self.by_store
        return value
