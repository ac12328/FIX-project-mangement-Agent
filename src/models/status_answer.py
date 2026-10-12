"""
StatusAnswer Pydantic model (Blueprint §22.2).
Returned by answer_status() — read-only, never written to the database.
"""

from __future__ import annotations

from pydantic import BaseModel, Field


class StatusAnswer(BaseModel):
    """Structured answer for a single-project status query (§22.2)."""

    # Core project identity
    project_id: str
    client_name: str | None = None
    project_status: str | None = None
    current_milestone: str | None = None
    ball_with: str | None = None
    next_action: str | None = None
    market: str | None = None

    # §22.2 contract fields
    days_since_last_material_change: int | None = None
    active_blockers: list[str] = Field(default_factory=list)
    recommended_action: str | None = None
    escalate_to_operator: bool = False
    escalation_reason: str | None = None
    confidence: float = Field(default=0.0, ge=0.0, le=1.0)

    # Context enrichment (retained from legacy dict)
    context_confidence: float | None = None
    pending_questions: list[str] = Field(default_factory=list)
    operator_instruction: str | None = None
    stale_memory_keys: list[str] = Field(default_factory=list)
    skill_observations: list[str] = Field(default_factory=list)
    skill_recommendations: list[str] = Field(default_factory=list)
    recent_evidence_count: int = 0
    llm_answer: str | None = None
