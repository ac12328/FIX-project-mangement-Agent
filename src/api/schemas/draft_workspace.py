"""Request contracts for the secure draft workspace API."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field


class WorkspacePatchRequest(BaseModel):
    expected_revision: int = Field(ge=1)
    lease_token: str | None = None
    patch: dict[str, Any]


class RevisionRequest(BaseModel):
    expected_revision: int = Field(ge=1)
    lease_token: str | None = None


class ConfirmReplyRequest(RevisionRequest):
    email_id: str


class CheckpointCreateRequest(BaseModel):
    reason: str = "manual"


class LeaseTokenRequest(BaseModel):
    lease_token: str


class ReviewRequest(BaseModel):
    reviewer_user_id: str


class CommentCreateRequest(BaseModel):
    body: str
    location_type: Literal["general", "subject", "recipient", "body", "attachment"] = "general"
    location_ref: str | None = None
    parent_comment_id: str | None = None


class CommentResolveRequest(BaseModel):
    resolved: bool = True


class AIProposalCreateRequest(BaseModel):
    action: Literal["rewrite", "tone", "shorten", "translate", "fact_check"]
    proposal: dict[str, Any]
    base_revision: int | None = None
    base_hash: str | None = None


class AIProposalApplyRequest(BaseModel):
    selected_changes: list[str] | None = None
    lease_token: str | None = None


class SendNowRequest(BaseModel):
    expected_revision: int = Field(ge=1)
    expected_hash: str | None = None
    confirmed_warnings: list[str] = Field(default_factory=list)


class SendLaterRequest(SendNowRequest):
    scheduled_for: str


class RetryRequest(BaseModel):
    confirmed_warnings: list[str] = Field(default_factory=list)


class RescheduleRequest(BaseModel):
    scheduled_for: str
