"""Typed pending operation persisted between agent turns."""

from __future__ import annotations

from pydantic import BaseModel, Field


class PendingOperation(BaseModel):
    operation_id: str
    operation_type: str
    operator_id: str
    chat_id: str
    project_id: str | None = None
    resource_key: str | None = None
    request_key: str | None = None
    payload: dict
    base_version: str | None = None
    status: str = "pending"
    phase: str = "queued"
    checkpoint: dict = Field(default_factory=dict)
    result: dict = Field(default_factory=dict)
    attempt_count: int = 0
    last_error: str | None = None
    retry_at: str | None = None
    lease_owner: str | None = None
    lease_expires_at: str | None = None
    updated_at: str | None = None
    trigger_kind: str = "operator"
    requested_commit: bool = False
    create_if_missing: bool = False
    trusted_surface: str | None = None
    proposed_update_id: str | None = None
    created_at: str
    expires_at: str
    resolved_at: str | None = None
    resolution: str | None = None
