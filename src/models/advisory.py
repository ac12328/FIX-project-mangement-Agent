"""
Advisory models — structured next-step recommendations.
"""

from typing import Literal

from pydantic import BaseModel


class NextStep(BaseModel):
    """A single recommended next action."""
    action: str
    reason: str
    priority: Literal["now", "soon", "when_ready"] = "soon"
    can_auto_draft: bool = False


class EscalationTarget(BaseModel):
    """Suggested team/person to escalate to."""
    team: str
    reason: str
    suggested_recipients: list[str] = []


class DraftSuggestion(BaseModel):
    """Suggestion for an email draft the agent can prepare."""
    purpose: str
    key_points: list[str] = []
    tone: str = "professional"


class Advisory(BaseModel):
    """Context-aware advisory output from the advisory engine."""
    work_type: Literal["project", "support"]
    situation_assessment: str
    next_steps: list[NextStep] = []
    escalation: EscalationTarget | None = None
    draft_suggestion: DraftSuggestion | None = None
    questions_for_operator: list[str] = []
    similar_past_cases: list[str] = []
    confidence: float = 0.5
    # Real evidence citations only; policy and wiki references are separate.
    evidence_ref: list[str] = []
    policy_ref: list[str] = []
    criterion_results: list[dict] = []
    db_conflicts: list[str] = []
