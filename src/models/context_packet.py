"""DEPRECATED: Use ContextFrame for assembly; context_layers for L2/L3 models.

ContextPacket was the Blueprint §4.3 structured packet. Production assembly
now uses ContextFrame via ContextEngine. Layer models live in
src/models/context_layers.py. This module keeps thin re-exports and a
compat ContextPacket for one release / test fixtures.
"""

from __future__ import annotations

from pydantic import BaseModel, Field

from src.models.context_layers import RelationshipContext, WorkingMemoryContext
from src.models.evidence import Evidence
from src.models.project import Project

__all__ = [
    "WorkingMemoryContext",
    "RelationshipContext",
    "ContextPacket",
]


class ContextPacket(BaseModel):
    """Deprecated structured packet — prefer ContextFrame.

    Layer 1: Project truth
    Layer 2: Working memory
    Layer 3: Relationship/preference
    Layer 4: Evidence
    Layer 5: Knowledge context (market-specific + global rules)
    """

    project: Project
    recent_evidence: list[Evidence] = Field(default_factory=list)
    working_memory: WorkingMemoryContext = Field(default_factory=WorkingMemoryContext)
    relationship: RelationshipContext = Field(default_factory=RelationshipContext)
    knowledge_context: str = ""
    context_confidence: float = Field(ge=0.0, le=1.0, default=1.0)
    missing_context_flags: list[str] = Field(default_factory=list)
