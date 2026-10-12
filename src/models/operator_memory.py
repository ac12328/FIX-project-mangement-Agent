"""Pydantic contracts for operator_memory store."""

from __future__ import annotations

from enum import Enum
from typing import Any, Literal

from pydantic import BaseModel, Field


class MemoryType(str, Enum):
    SEMANTIC = "semantic"
    PROCEDURAL = "procedural"
    FORBIDDEN = "forbidden"
    EPISODIC_SNIPPET = "episodic_snippet"


MemoryScope = Literal[
    "global",
    "project",
    "chat",
    "counterparty",
    "contact",
    "organisation",
    "work_item",
    "topic",
    "calendar_event",
]


class OperatorMemoryEntry(BaseModel):
    operator_id: str
    scope: MemoryScope
    scope_id: str = "*"
    memory_type: MemoryType
    memory_key: str
    memory_value: dict[str, Any] = Field(default_factory=dict)
    source: str = "operator"
    active: bool = True