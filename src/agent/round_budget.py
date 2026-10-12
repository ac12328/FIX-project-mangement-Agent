"""Round budget resolution by intent and request complexity."""

from __future__ import annotations

import re

from src.agent.intent import detect_project_id, is_complex_request

_DEFAULT = 8
_QUERY = 4
_EMAIL = 5
_COMPLEX = 12
# Single-project field updates should not burn full complex budget
_FOCUSED_UPDATE = 6

# Worst-case ceiling for HTTP timeouts and back-pressure calculations.
MAX_AGENT_ROUNDS = _COMPLEX

_FOCUSED_UPDATE_RE = re.compile(
    r"(?:"
    r"go[\s-]?live|m8_?golive|to\s+go[\s-]?live"
    r"|update\s+(?:the\s+)?project"
    r"|set\s+(?:milestone|ball|status|next_action)"
    r"|更新|推進|改\s*ball|milestone"
    r")",
    re.IGNORECASE,
)


def is_focused_project_update(user_text: str) -> bool:
    """True when message is a single-project field/go-live update (not multi-project)."""
    if not user_text or not user_text.strip():
        return False
    if not detect_project_id(user_text):
        return False
    if not _FOCUSED_UPDATE_RE.search(user_text):
        return False
    # Multi-project portfolio work still gets full complex budget
    lower = user_text.lower()
    if "all projects" in lower or "portfolio" in lower:
        return False
    return True


def resolve_max_rounds(intent: str, user_text: str) -> int:
    if intent == "query":
        return _QUERY
    if intent == "email":
        return _EMAIL
    # Focused go-live / field updates: cap below full complex budget so
    # orchestrator+delegate thrash cannot run to 12 by default.
    if is_focused_project_update(user_text):
        return _FOCUSED_UPDATE
    if is_complex_request(user_text):
        return _COMPLEX
    return _DEFAULT
