"""Shared helpers for tool progress display (Telegram bubbles, logging)."""

from __future__ import annotations

TOOL_STEP_PREFIX = "Execute tool: "
SUB_TASK_STEP_PREFIX = "Sub-task: "

_SENSITIVE_KEYS = frozenset({
    "password", "token", "secret", "api_key", "apikey", "authorization",
    "credential", "private_key", "access_token", "refresh_token",
})


def parse_tool_step(step_name: str) -> str | None:
    if not step_name.startswith(TOOL_STEP_PREFIX):
        return None
    name = step_name[len(TOOL_STEP_PREFIX) :].strip()
    return name or None


def format_sub_task_step(specialist_type: str) -> str:
    """Progress step name for orchestrator specialist delegation."""
    return f"{SUB_TASK_STEP_PREFIX}{specialist_type}"


def parse_sub_task_step(step_name: str) -> str | None:
    if not step_name.startswith(SUB_TASK_STEP_PREFIX):
        return None
    name = step_name[len(SUB_TASK_STEP_PREFIX) :].strip()
    return name or None


def format_tool_arg_preview(arguments: dict) -> str | None:
    """Build a safe, short argument preview for progress bubbles."""
    if not arguments:
        return None
    parts: list[str] = []
    for key, value in arguments.items():
        if key.lower() in _SENSITIVE_KEYS:
            continue
        text = str(value).strip()
        if not text:
            continue
        if len(text) > 60:
            text = text[:57] + "..."
        parts.append(f"{key}={text}")
        if len(parts) >= 2:
            break
    if not parts:
        return None
    preview = ", ".join(parts)
    return preview if len(preview) <= 80 else preview[:77] + "..."