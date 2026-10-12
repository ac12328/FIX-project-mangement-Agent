"""Typed action intent contracts for approval-bound operations."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field

RiskClass = Literal["low", "medium", "high", "critical"]
ActionStatus = Literal[
    "pending",
    "approved",
    "rejected",
    "executing",
    "executed",
    "uncertain",
    "failed",
    "expired",
    "invalidated",
]


class ActionIntent(BaseModel):
    action_id: str
    operator_id: str
    action_type: str
    risk_class: RiskClass
    target_type: str
    target_id: str
    target_version: str | None = None
    payload: dict[str, Any] = Field(default_factory=dict)
    payload_hash: str
    status: ActionStatus
    created_by_surface: str
    approved_by: str | None = None
    created_at: str
    expires_at: str
    approved_at: str | None = None
    executed_at: str | None = None
    result_ref: str | None = None
    error: str | None = None
    idempotency_key: str | None = None
