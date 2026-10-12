"""Layered stall detection for the agent harness."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass

_PROJECT_ID_RE = re.compile(
    r"\b(?:[A-Z]{2}\d{4}|[A-Z][A-Z0-9_]+-\d{2,})\b",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class StallSignal:
    kind: str
    detail: str


def _normalize_tool_content(content: str) -> str:
    text = content.strip().lower()
    text = _PROJECT_ID_RE.sub("", text)
    text = re.sub(r"\s+", " ", text)
    return text[:500]


def _tool_results_from_messages(messages: list[dict]) -> list[str]:
    return [
        m.get("content") or ""
        for m in messages
        if m.get("role") == "tool" and m.get("content")
    ]


def detect_stall(
    tool_history: list[tuple[str, ...]],
    messages: list[dict],
) -> StallSignal | None:
    if not tool_history:
        return None

    # Layer 1: identical name tuples ×3
    if len(tool_history) >= 3 and tool_history[-1] == tool_history[-2] == tool_history[-3]:
        return StallSignal(
            kind="identical_calls",
            detail=f"tools={tool_history[-1]}",
        )

    # Layer 2: same primary tool ≥4 times in last 5 rounds
    recent = tool_history[-5:]
    if recent:
        last_tool = recent[-1][0] if recent[-1] else ""
        if last_tool and sum(1 for rnd in recent if rnd and rnd[0] == last_tool) >= 4:
            return StallSignal(kind="same_tool_repeat", detail=f"tool={last_tool}")

    # Layer 3: consecutive tool errors with same prefix
    tool_results = _tool_results_from_messages(messages)
    if len(tool_results) >= 2:
        last_two = tool_results[-2:]
        if all(t.strip().lower().startswith("error:") for t in last_two):
            return StallSignal(kind="error_repeat", detail=last_two[-1][:120])

    # Layer 4: near-identical tool result content
    if len(tool_results) >= 3:
        a = _normalize_tool_content(tool_results[-1])
        b = _normalize_tool_content(tool_results[-2])
        c = _normalize_tool_content(tool_results[-3])
        if a and a == b == c:
            return StallSignal(kind="content_repeat", detail=a[:120])

    return None