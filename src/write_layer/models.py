"""
WriteResult — outcome of safe_write_project().
Replaces the old WriteDecision + ProposedUpdate pipeline.
"""

from dataclasses import dataclass, field


@dataclass
class WriteResult:
    """Result of a safe_write_project() call."""

    outcome: str  # "accepted" | "rejected" | "needs_review" | "no_change"
    applied_fields: dict = field(default_factory=dict)
    rejection_reason: str | None = None
    needs_review_reason: str | None = None
