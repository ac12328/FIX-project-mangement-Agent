"""Typed immutable contracts for the learning observation ledger."""
from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum
from typing import Any

from pydantic import BaseModel, Field, field_validator


class ObservationSourceType(str, Enum):
    CONVERSATION_FLUSH = "conversation_flush"
    OPERATOR_FEEDBACK = "operator_feedback"
    DRAFT_EDIT = "draft_edit"
    EMAIL_SENT = "email_sent"
    CLIENT_REPLY = "client_reply"
    TOOL_FAILURE = "tool_failure"
    MANUAL_MEMORY = "manual_memory"
    WIKI_INGESTION = "wiki_ingestion"
    MIGRATION = "migration"


class TrustTier(str, Enum):
    UNTRUSTED = "untrusted"
    EXTERNAL = "external"
    SYSTEM = "system"
    OPERATOR = "operator"


class Sensitivity(str, Enum):
    PUBLIC = "public"
    INTERNAL = "internal"
    CONFIDENTIAL = "confidential"
    RESTRICTED = "restricted"


class LearningObservation(BaseModel):
    """Append-only, sanitized source evidence; never an active memory assertion."""

    observation_id: str
    source_type: ObservationSourceType
    source_id: str
    idempotency_key: str
    operator_id: str
    project_id: str | None = None
    contact_id: str | None = None
    counterparty_id: str | None = None
    artifact_type: str | None = None
    artifact_id: str | None = None
    trace_id: str | None = None
    output_revision_id: str | None = None
    raw_payload: dict[str, Any] = Field(default_factory=dict)
    normalized_text: str
    content_hash: str
    sensitivity: Sensitivity = Sensitivity.INTERNAL
    trust_tier: TrustTier = TrustTier.UNTRUSTED
    occurred_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    created_at: datetime | None = None
    is_tombstone: bool = False
    tombstone_of_observation_id: str | None = None
    tombstone_reason: str | None = None

    @field_validator("observation_id", "source_id", "idempotency_key", "operator_id", "content_hash")
    @classmethod
    def _non_empty(cls, value: str) -> str:
        if not value or not value.strip():
            raise ValueError("observation identifiers and content hash are required")
        return value

    @field_validator("normalized_text")
    @classmethod
    def _text_required_unless_tombstone(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("normalized text is required")
        return value
