"""Typed contracts for the Learning Center control-plane API."""
from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field, model_validator


class Page(BaseModel):
    items: list[dict[str, Any]]
    total: int = Field(ge=0)
    limit: int = Field(ge=1)
    offset: int = Field(ge=0)


class CandidateActionRequest(BaseModel):
    action: Literal[
        "approve", "reject", "edit_and_approve", "narrow_scope",
        "request_evidence", "merge", "quarantine",
    ]
    reason: str = Field(min_length=1)
    idempotency_key: str = Field(min_length=1)
    confirmed: bool = False
    confirmation_token: str | None = None
    expected_revision: str | None = None
    proposed_value: Any | None = None
    scope_type: str | None = None
    scope_id: str | None = None
    suggested_expiry: str | None = None
    merge_candidate_id: str | None = None
    merge_candidate_version: int | None = Field(default=None, ge=1)


class FactActionRequest(BaseModel):
    action: Literal["deactivate", "revoke", "supersede", "change_expiry", "change_scope", "rollback"]
    reason: str = Field(min_length=1)
    idempotency_key: str = Field(min_length=1)
    confirmed: bool = False
    confirmation_token: str | None = None
    expected_revision: str | None = None
    operation_id: str | None = None
    replacement_candidate_id: str | None = None
    replacement_candidate_version: int | None = Field(default=None, ge=1)
    scope_type: str | None = None
    scope_id: str | None = None
    suggested_expiry: str | None = None


class ConflictActionRequest(BaseModel):
    action: Literal["resolve", "merge", "supersede"]
    reason: str = Field(min_length=1)
    idempotency_key: str = Field(min_length=1)
    confirmed: bool = False
    confirmation_token: str | None = None
    expected_revision: str | None = None
    candidate_id: str | None = None
    candidate_version: int | None = Field(default=None, ge=1)


class JobActionRequest(BaseModel):
    action: Literal["retry", "quarantine", "replay"]
    reason: str = Field(min_length=1)
    idempotency_key: str = Field(min_length=1)
    confirmed: bool = False
    confirmation_token: str | None = None
    expected_revision: str | None = None
    extractor_version: str | None = None

    @model_validator(mode="after")
    def require_replay_version(self):
        if self.action == "replay" and not self.extractor_version:
            raise ValueError("replay requires extractor_version")
        return self


class KillSwitchRequest(BaseModel):
    value: bool
    reason: str = Field(min_length=1)
    idempotency_key: str = Field(min_length=1)
    confirmed: bool = False
    confirmation_token: str | None = None
    expected_revision: str | None = None


class ActionResponse(BaseModel):
    success: bool = True
    action: str
    event_type: str
    candidate_id: str | None = None
    candidate_version: int | None = None
    decision_id: str | None = None
    fact_id: str | None = None
    conflict_id: str | None = None
    job_id: str | None = None
    operation_id: str | None = None
    status: str | None = None
    effective: bool | None = None
