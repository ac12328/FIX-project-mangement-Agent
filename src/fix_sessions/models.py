"""Typed contracts for canonical FIX session configuration."""

from __future__ import annotations

from enum import Enum
from typing import Any

from pydantic import BaseModel, Field, field_validator


class FixEnvironment(str, Enum):
    UAT = "UAT"
    PRODUCTION = "PRODUCTION"


class RouteRole(str, Enum):
    PRIMARY = "primary"
    BACKUP = "backup"
    DR = "dr"


class CandidateStatus(str, Enum):
    PENDING_REVIEW = "pending_review"
    CONFIRMED = "confirmed"
    REJECTED = "rejected"
    SUPERSEDED = "superseded"


class ConfirmationMode(str, Enum):
    AUTO = "auto"
    OPERATOR = "operator"
    BACKFILL = "backfill"


class AuthResult(str, Enum):
    PASS = "pass"
    FAIL = "fail"
    UNKNOWN = "unknown"


class EmailAuthentication(BaseModel):
    spf: AuthResult = AuthResult.UNKNOWN
    dkim: AuthResult = AuthResult.UNKNOWN
    dmarc: AuthResult = AuthResult.UNKNOWN
    aligned_sender: bool = False
    raw_summary: str | None = None

    @property
    def fully_authenticated(self) -> bool:
        return (
            self.spf == AuthResult.PASS
            and self.dkim == AuthResult.PASS
            and self.dmarc == AuthResult.PASS
            and self.aligned_sender
        )


class FixSessionEndpoint(BaseModel):
    route_role: RouteRole = RouteRole.PRIMARY
    host: str = Field(min_length=1, max_length=255)
    port: int = Field(ge=1, le=65535)
    priority: int = Field(default=1, ge=1)
    transport: str = Field(default="tcp", max_length=32)

    @field_validator("host")
    @classmethod
    def normalize_host(cls, value: str) -> str:
        return value.strip().lower()


class FixSessionSnapshot(BaseModel):
    project_id: str = Field(min_length=1, max_length=100)
    environment: FixEnvironment
    session_key: str = Field(default="order_entry", pattern=r"^[a-z0-9][a-z0-9_-]{0,63}$")
    begin_string: str | None = Field(default=None, max_length=16)
    sender_comp_id: str = Field(min_length=1, max_length=128)
    target_comp_id: str = Field(min_length=1, max_length=128)
    heartbeat_seconds: int | None = Field(default=None, ge=1, le=3600)
    reset_seq_num_on_logon: bool | None = None
    account_reference: str | None = Field(default=None, max_length=255)
    username_secret_reference: str | None = Field(default=None, max_length=512)
    password_secret_reference: str | None = Field(default=None, max_length=512)
    endpoints: list[FixSessionEndpoint] = Field(min_length=1)
    metadata: dict[str, Any] = Field(default_factory=dict)

    @field_validator("project_id", "sender_comp_id", "target_comp_id")
    @classmethod
    def strip_required(cls, value: str) -> str:
        return value.strip()


class FixSessionCandidate(BaseModel):
    candidate_id: str
    snapshot: FixSessionSnapshot
    source_email_id: str | None = None
    source_attachment_id: str | None = None
    evidence_id: str | None = None
    sender_email: str | None = None
    sender_policy_id: str | None = None
    authentication: EmailAuthentication = Field(default_factory=EmailAuthentication)
    confidence: float = Field(default=0.0, ge=0.0, le=1.0)
    completeness: float = Field(default=0.0, ge=0.0, le=1.0)
    changed_fields: list[str] = Field(default_factory=list)
    inherited_fields: list[str] = Field(default_factory=list)
    conflict_reasons: list[str] = Field(default_factory=list)
    field_sources: dict[str, dict[str, Any]] = Field(default_factory=dict)
    status: CandidateStatus = CandidateStatus.PENDING_REVIEW
    received_at: str | None = None
