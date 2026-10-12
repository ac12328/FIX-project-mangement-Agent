"""Typed tool results that influence agent loop control flow."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Any


class LoopActionKind(str, Enum):
    CONFIRM = "confirm"
    RESULT = "result"


@dataclass(frozen=True)
class LoopAction:
    """Structured loop control signal from a tool handler."""

    kind: LoopActionKind
    session_id: str | None = None
    summary: str | None = None
    payload: dict[str, Any] | None = None

    @classmethod
    def from_tool_result(cls, result: Any) -> LoopAction | None:
        if isinstance(result, dict) and result.get("action") == "confirm":
            return cls(
                kind=LoopActionKind.CONFIRM,
                session_id=result.get("session_id"),
                summary=result.get("summary"),
                payload=dict(result),
            )
        return None

    def to_tool_dict(self) -> dict[str, Any]:
        if self.payload is not None:
            return dict(self.payload)
        data: dict[str, Any] = {"action": self.kind.value}
        if self.session_id is not None:
            data["session_id"] = self.session_id
        if self.summary is not None:
            data["summary"] = self.summary
        return data