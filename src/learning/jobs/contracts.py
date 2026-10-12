"""Typed contracts for durable learning jobs crossing module boundaries."""
from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Any

from pydantic import BaseModel, Field


class LearningJobState(str, Enum):
    QUEUED = "queued"
    PROCESSING = "processing"
    RETRY = "retry"
    COMPLETED = "completed"
    QUARANTINED = "quarantined"
    DEAD_LETTER = "dead_letter"


class LearningJob(BaseModel):
    job_id: str
    observation_id: str
    job_type: str
    extractor_version: str
    state: LearningJobState
    attempts: int = 0
    available_at: datetime
    lease_token: str | None = None
    leased_at: datetime | None = None
    lease_expires_at: datetime | None = None
    last_error: str | None = None
    payload: dict[str, Any] = Field(default_factory=dict)
    result: dict[str, Any] | None = None
    created_at: datetime
    completed_at: datetime | None = None


class EffectPhase(str, Enum):
    """Governance phase of a DB-only effect; finalization never performs external I/O."""

    CAPTURE_ONLY = "capture_only"
    CANDIDATE_PROPOSAL = "candidate_proposal"
    ACTIVE_MEMORY = "active_memory"
    GLOBAL_MEMORY = "global_memory"
    PROCEDURAL_MEMORY = "procedural_memory"
    WIKI_PUBLICATION = "wiki_publication"
    EXTERNAL_ACTION = "external_action"


class JobResult(BaseModel):
    """Pure handler output. A registered DB-only materializer may persist it atomically."""

    effect_type: str = "capture_only"
    effect_phase: EffectPhase = EffectPhase.CAPTURE_ONLY
    payload: dict[str, Any] = Field(default_factory=dict)
    quarantine_reason: str | None = None

    def canonical_effect_key(self, job: "LearningJob") -> str:
        return f"{job.observation_id}:{job.job_type}:{job.extractor_version}:{self.effect_type}"


class FailureOutcome(BaseModel):
    state: LearningJobState
    delay_seconds: int = 0


class WorkerRunResult(BaseModel):
    claimed: int = 0
    completed: int = 0
    retried: int = 0
    quarantined: int = 0
    dead_lettered: int = 0
    stale: int = 0
