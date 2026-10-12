"""Neutral tool contract definitions, separate from registry assembly."""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from typing import Any


@dataclass
class ToolContract:
    """Static contract for a tool."""

    name: str
    description: str
    input_schema: dict[str, Any]
    output_schema: dict[str, Any]
    allowed_task_types: list[str]
    allowed_contexts: list[str] = field(default_factory=lambda: ["lead_agent"])
    llm_visible: bool = True
    version: str = "1.0"
    side_effect_type: str = "none"
    requires_operator_approval: bool = False
    timeout_seconds: int = 30
    retry_policy: str = "none"
    idempotency_mode: str = "none"
    minimum_context_confidence: float | None = None
    fallback_behaviour: str = "return_error"
    log_input: bool = True
    log_output: bool = True
    audit_required: bool = False
    query_safe: bool = False

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), indent=2)
