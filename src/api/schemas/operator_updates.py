"""HTTP contracts for Workspace operator project updates (PR-2 uses these)."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field


class OperatorUpdateStageRequest(BaseModel):
    thread_id: str
    fields: dict[str, str]
    reason: str = ""
    evidence_ref: str | None = None


class OperatorUpdateConfirmRequest(BaseModel):
    thread_id: str
    reason: str | None = None
    evidence_ref: str | None = None


class OperatorUpdateCardResponse(BaseModel):
    operation_id: str
    status: str
    kind: str
    high_risk: bool
    expires_at: str | None = None
    base_version: str | None = None
    part: dict[str, Any] | None = None
    missing_criteria: list[dict[str, Any]] = Field(default_factory=list)
    current_values: dict[str, Any] = Field(default_factory=dict)
    fields: dict[str, str] = Field(default_factory=dict)
    reason: str = ""
    transition: str = "adjacent"


class OperatorUpdateReceipt(BaseModel):
    """Committed or cancelled project-update command outcome for HTTP clients."""

    ok: bool
    outcome: str
    committed: bool
    operation_id: str
    project_id: str
    text: str
    error: str | None = None


class OperatorUpdateErrorDetail(BaseModel):
    code: str
    message: str


class OperatorUpdateDomainErrorResponse(BaseModel):
    detail: OperatorUpdateErrorDetail


class OperatorUpdatePermissionErrorResponse(BaseModel):
    """RBAC denials retain FastAPI's plain-string detail."""

    detail: str


class OperatorUpdateValidationError(BaseModel):
    loc: list[str | int]
    msg: str
    type: str


class OperatorUpdateValidationErrorResponse(BaseModel):
    """FastAPI/Pydantic request validation errors use a detail array."""

    detail: list[OperatorUpdateValidationError]
