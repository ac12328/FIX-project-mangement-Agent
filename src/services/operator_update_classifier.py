"""Deterministic Workspace speech-act classifier for milestone writes."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Literal

from src.agent.guards import _milestone_value
from src.agent.intent import detect_project_id
from src.models.enums import Milestone

from src.assistant.contracts import AssistantIntent

# Bare "ready" is too broad: pasted PCT emails say "ready for production launch".
_ASSESS_RE = re.compile(
    r"(?i)\b(readiness|assess(?:ment)?s?|is\s+\S+\s+ready)\b"
)
_QUESTION_RE = re.compile(r"(?i)\b(should|can|could|may)\s+we\b")
_STATUS_Q_RE = re.compile(r"(?i)\bis\s+\S+\s+(?:at|on|still\s+at)?\s*m[1-8]\b")
_WRITE_VERB_RE = re.compile(
    r"(?i)\b(update|set|change|advance|move|revert|fail|推[進到至]|改[成爲为到]|回退|失敗)\b"
)
_TARGET_RE = re.compile(
    r"(?:to|[=→]|到|去|back\s+to|回)\s*m[1-8]\b"
    r"|milestone\s+(?:to|[=→]|到)\s*"
    r"|current_milestone",
    re.IGNORECASE,
)
_NEXT_ACTION_RE = re.compile(r"(?i)next[_\s-]?action\s*[:=]\s*(.+)$")
_FULL_ENUM_RE = re.compile(
    r"\b(M[1-8]_[A-Za-z0-9]+)\b"
)
_SHORT_MS_RE = re.compile(r"(?i)\bm([1-8])\b")


@dataclass(frozen=True)
class WorkspaceWriteIntent:
    project_id: str
    to_milestone: str
    next_action: str | None
    reason_text: str
    source: Literal["text", "structured_intent"]


def _resolve_milestone(token: str | None) -> str | None:
    if not token:
        return None
    raw = token.strip()
    try:
        return Milestone(raw).value
    except ValueError:
        pass
    for item in Milestone:
        if item.name == raw or item.value.casefold() == raw.casefold():
            return item.value
    mapped = _milestone_value(raw)
    if mapped:
        return mapped
    short = _SHORT_MS_RE.fullmatch(raw)
    if short:
        return _milestone_value(short.group(1))
    return None


def _milestone_from_text(message: str) -> str | None:
    full = _FULL_ENUM_RE.search(message)
    if full:
        resolved = _resolve_milestone(full.group(1))
        if resolved:
            return resolved
    short = _SHORT_MS_RE.search(message)
    if short:
        return _milestone_value(short.group(1))
    return None


def _line_is_question(line: str) -> bool:
    stripped = line.strip()
    if not stripped:
        return False
    return bool(
        stripped.endswith("?")
        or _QUESTION_RE.search(stripped)
        or _STATUS_Q_RE.search(stripped)
    )


def _has_imperative_write(text: str) -> bool:
    """True when a non-question line is an explicit milestone write.

    Assessment vocabulary in pasted evidence (for example Demo Provisioning 'ready for
    production launch') must not veto 'Update to M7'.
    """
    lines = [item.strip() for item in (text or "").splitlines() if item.strip()]
    if not lines:
        return False
    for line in lines:
        if _line_is_question(line):
            continue
        if _WRITE_VERB_RE.search(line) and _TARGET_RE.search(line):
            return True
    return False


def _is_read_speech_act(text: str) -> bool:
    if _has_imperative_write(text):
        return False
    return bool(
        _ASSESS_RE.search(text)
        or _QUESTION_RE.search(text)
        or _STATUS_Q_RE.search(text)
    )


def classify_workspace_operator_write(
    message: str,
    *,
    scope_mode: str,
    project_ids: list[str],
    intent: AssistantIntent | None = None,
) -> WorkspaceWriteIntent | None:
    text = message or ""
    source: Literal["text", "structured_intent"] = "text"
    to_milestone = None
    project_id = None
    next_action = None

    if (
        intent is not None
        and intent.type == "project_update"
        and str(intent.field or "").lower() in {"current_milestone", "milestone"}
    ):
        source = "structured_intent"
        to_milestone = _resolve_milestone(str(intent.value or ""))
        if intent.project_ids:
            project_id = intent.project_ids[0]
    else:
        if _is_read_speech_act(text):
            return None
        if not _WRITE_VERB_RE.search(text) or not _TARGET_RE.search(text):
            return None
        to_milestone = _milestone_from_text(text)

    if to_milestone is None:
        return None

    if not project_id:
        project_id = detect_project_id(text)
    if not project_id and scope_mode == "projects" and len(project_ids) == 1:
        project_id = project_ids[0]
    if not project_id:
        return None

    explicit_next = _NEXT_ACTION_RE.search(text)
    if explicit_next:
        next_action = explicit_next.group(1).strip() or None
    if intent is not None and getattr(intent, "value", None) and source == "structured_intent":
        pass

    reason = text
    for pattern in (_WRITE_VERB_RE, _TARGET_RE, _FULL_ENUM_RE, _SHORT_MS_RE, _NEXT_ACTION_RE):
        reason = pattern.sub(" ", reason)
    reason_text = " ".join(reason.split()).strip(" —-:;")
    return WorkspaceWriteIntent(
        project_id=str(project_id).strip().upper(),
        to_milestone=to_milestone,
        next_action=next_action,
        reason_text=reason_text,
        source=source,
    )
