"""
Context layer models (L2 working memory, L3 relationship).

Used by Context Engine layer loaders and structured draft/status helpers.
Assembly output for agents is ContextFrame (see src/models/context_frame.py).
Memory ownership: docs/domain/MEMORY_OWNERSHIP.md.
"""

from __future__ import annotations

from pydantic import BaseModel, Field


class WorkingMemoryContext(BaseModel):
    """Layer 2: Working memory entries for the project."""

    recent_thread_summary: str | None = None
    pending_questions: list[str] = Field(default_factory=list)
    open_decisions: list[str] = Field(default_factory=list)
    latest_operator_instruction: str | None = None
    stale_keys: list[str] = Field(default_factory=list)


class RelationshipContext(BaseModel):
    """Layer 3: Relationship and preference data for a contact+project pair."""

    preferred_greeting: str | None = None
    tone: str | None = None
    response_length_preference: str | None = None
    preferred_language: str | None = None
    avoid_phrases: list[str] = Field(default_factory=list)
    positive_signals: list[str] = Field(default_factory=list)


__all__ = [
    "WorkingMemoryContext",
    "RelationshipContext",
]
