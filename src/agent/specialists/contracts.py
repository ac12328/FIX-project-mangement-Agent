from __future__ import annotations

from enum import Enum
from typing import Literal

from pydantic import BaseModel, Field

from src.agent.tool_receipts import ToolExecutionReceipt


class SpecialistType(str, Enum):
    STATUS = "status"
    RESEARCH = "research"
    WRITE = "write"
    DRAFT = "draft"
    PORTFOLIO = "portfolio"
    SUPPORT = "support"
    EMAIL = "email"


class ProposedWrite(BaseModel):
    field: str
    value: str
    evidence_ref: str | None = None


SpecialistCompletionStatus = Literal[
    "completed",
    "partial",
    "timed_out",
    "failed",
    "exhausted",
    "unverified",
]


class SpecialistFailureEvent(BaseModel):
    """One normalized control-plane failure, independent of successful evidence."""

    code: str
    phase: str
    message: str
    retryable: bool = False


class SpecialistEvidence(BaseModel):
    """Bounded evidence retained in a specialist result and resumable session."""

    source_type: str
    locator: str
    authority: str = "reported"
    status: str | None = None
    content_hash: str | None = None
    excerpt: str | None = None
    claim_coverage: list[str] = Field(default_factory=list)


class SpecialistReport(BaseModel):
    specialist_type: SpecialistType
    ok: bool = True
    summary: str = ""
    findings: list[str] = Field(default_factory=list)
    sources: list[str] = Field(default_factory=list)
    proposed_writes: list[ProposedWrite] = Field(default_factory=list)
    evidence_refs: list[str] = Field(default_factory=list)
    draft_id: str | None = None
    confidence: float = 1.0
    error: str | None = None
    completion_status: SpecialistCompletionStatus = "completed"
    exit_reason: str = "model_stop"
    outcome: str = "completed"
    committed: bool = False
    operation_id: str | None = None
    execution_receipts: list[ToolExecutionReceipt] = Field(default_factory=list)
    evidence: list[SpecialistEvidence] = Field(default_factory=list)
    failure_events: list[SpecialistFailureEvent] = Field(default_factory=list)
    # Host uses this for delegate(..., resume_from=session_id).
    session_id: str | None = None
    # Stable alias for clients that model resumability independently of storage.
    resume_token: str | None = None
    skill_id: str | None = None


class DelegateRequest(BaseModel):
    specialist_type: SpecialistType = SpecialistType.RESEARCH
    task: str
    project_id: str | None = None
    skill_id: str | None = None
    resume_from: str | None = None