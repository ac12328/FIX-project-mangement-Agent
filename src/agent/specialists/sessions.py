"""
Specialist session store — resume_from continuation for delegate.

Design (best practice):
  - Dedicated specialist_sessions table (not thread_artifacts).
  - Same skill always resumable; cross-skill only via allowlist.
  - On resume, tools re-render for the *target* skill (transcript kept).
  - TTL expires sessions; operator/thread scoped.
"""

from __future__ import annotations

import json
import logging
import sqlite3
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any

logger = logging.getLogger(__name__)

# Cross-skill resume pairs: (from_skill_id, to_skill_id)
# Same skill is always allowed without listing here.
RESUME_CROSS_SKILL_ALLOWLIST: frozenset[tuple[str, str]] = frozenset({
    ("research", "draft"),
    ("research", "project_write"),
    ("research", "email_to_truth"),
    ("research", "new_project"),
    ("email_to_truth", "draft"),
    ("status", "draft"),
    ("status", "project_write"),
})

DEFAULT_TTL_SECONDS = 3600
_MAX_TOOL_CONTENT_CHARS = 6000
_MAX_MESSAGES_STORED = 40
_MAX_CHECKPOINT_RECEIPTS = 24
_MAX_CHECKPOINT_GUARDS = 40
_MAX_CHECKPOINT_EXCERPT_CHARS = 1200
_SESSION_STATUSES = frozenset({
    "in_progress",
    "completed",
    "partial",
    "timed_out",
    "exhausted",
    "failed",
    "unverified",
})


@dataclass(frozen=True)
class SpecialistSession:
    session_id: str
    operator_id: str
    thread_id: str
    skill_id: str
    specialist_type: str
    project_id: str | None
    parent_session_id: str | None
    messages: list[dict[str, Any]]
    tool_history: list[Any]
    status: str
    created_at: str
    expires_at: str
    checkpoint: dict[str, Any] = field(default_factory=dict)
    checkpoint_seq: int = 0
    updated_at: str = ""


class ResumeError(Exception):
    """Host-facing resume rejection."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


def can_resume(from_skill_id: str, to_skill_id: str) -> bool:
    """True if target skill may continue a prior specialist transcript."""
    src = (from_skill_id or "").strip().lower()
    dst = (to_skill_id or "").strip().lower()
    if not src or not dst:
        return False
    if src == dst:
        return True
    return (src, dst) in RESUME_CROSS_SKILL_ALLOWLIST


def compact_messages_for_storage(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Trim transcript size while keeping tool evidence usable on resume."""
    if not messages:
        return []
    # Keep the last N messages (always include leading system if present)
    trimmed = list(messages)
    if len(trimmed) > _MAX_MESSAGES_STORED:
        head: list[dict[str, Any]] = []
        if trimmed[0].get("role") == "system":
            head = [trimmed[0]]
            body = trimmed[1:]
        else:
            body = trimmed
        keep = _MAX_MESSAGES_STORED - len(head)
        trimmed = head + body[-keep:]

    out: list[dict[str, Any]] = []
    for msg in trimmed:
        m = dict(msg)
        content = m.get("content")
        if isinstance(content, str) and len(content) > _MAX_TOOL_CONTENT_CHARS:
            m["content"] = (
                content[:_MAX_TOOL_CONTENT_CHARS]
                + f"\n…[truncated {len(content) - _MAX_TOOL_CONTENT_CHARS} chars for session store]"
            )
        out.append(m)
    return out


def _compact_checkpoint(checkpoint: dict[str, Any] | None) -> dict[str, Any]:
    """Keep durable control-plane state bounded and JSON serializable."""
    if not isinstance(checkpoint, dict):
        return {}
    compact = {
        "schema_version": int(checkpoint.get("schema_version") or 1),
        "phase": str(checkpoint.get("phase") or "unknown"),
        "round_index": int(checkpoint.get("round_index") or 0),
        "exit_reason": (
            str(checkpoint["exit_reason"])
            if checkpoint.get("exit_reason") is not None
            else None
        ),
    }
    messages = checkpoint.get("messages")
    if isinstance(messages, list):
        compact["messages"] = compact_messages_for_storage(messages)
    tool_history = checkpoint.get("tool_history")
    if isinstance(tool_history, list):
        compact["tool_history"] = tool_history[-_MAX_MESSAGES_STORED:]
    receipts = checkpoint.get("execution_receipts")
    if isinstance(receipts, list):
        compact["execution_receipts"] = receipts[-_MAX_CHECKPOINT_RECEIPTS:]
    guards = checkpoint.get("guard_results")
    if isinstance(guards, list):
        compact["guard_results"] = guards[-_MAX_CHECKPOINT_GUARDS:]
    report = checkpoint.get("report")
    if isinstance(report, dict):
        compact["report"] = report
    failure = checkpoint.get("failure")
    if isinstance(failure, dict):
        compact["failure"] = failure
    return json.loads(json.dumps(compact, ensure_ascii=False, default=str))


def store_session(
    conn: sqlite3.Connection,
    *,
    messages: list[dict[str, Any]],
    skill_id: str,
    specialist_type: str,
    operator_id: str,
    thread_id: str,
    project_id: str | None = None,
    parent_session_id: str | None = None,
    tool_history: list[Any] | None = None,
    checkpoint: dict[str, Any] | None = None,
    status: str = "completed",
    ttl_seconds: int = DEFAULT_TTL_SECONDS,
    session_id: str | None = None,
) -> str:
    """Create or checkpoint a specialist session and return its stable ID."""
    normalized_status = str(status or "").strip().casefold()
    if normalized_status not in _SESSION_STATUSES:
        raise ValueError(f"unsupported specialist session status: {status}")
    session_id = session_id or f"spc_{uuid.uuid4().hex[:16]}"
    now = datetime.now(timezone.utc)
    expires = now + timedelta(seconds=max(60, ttl_seconds))
    payload = compact_messages_for_storage(messages)
    compact_checkpoint = _compact_checkpoint(checkpoint)
    conn.execute(
        """
        INSERT INTO specialist_sessions (
            session_id, operator_id, thread_id, skill_id, specialist_type,
            project_id, parent_session_id, messages_json, tool_history_json,
            checkpoint_json, checkpoint_seq, status, created_at, updated_at,
            expires_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?, ?, ?, ?)
        ON CONFLICT(session_id) DO UPDATE SET
            operator_id = excluded.operator_id,
            thread_id = excluded.thread_id,
            skill_id = excluded.skill_id,
            specialist_type = excluded.specialist_type,
            project_id = excluded.project_id,
            parent_session_id = excluded.parent_session_id,
            messages_json = excluded.messages_json,
            tool_history_json = excluded.tool_history_json,
            checkpoint_json = excluded.checkpoint_json,
            checkpoint_seq = specialist_sessions.checkpoint_seq + 1,
            status = excluded.status,
            updated_at = excluded.updated_at,
            expires_at = excluded.expires_at
        """,
        (
            session_id,
            operator_id or "default_operator",
            thread_id or "default",
            (skill_id or specialist_type or "research").strip().lower(),
            (specialist_type or "research").strip().lower(),
            project_id,
            parent_session_id,
            json.dumps(payload, ensure_ascii=False, default=str),
            json.dumps(tool_history or [], ensure_ascii=False, default=str),
            json.dumps(compact_checkpoint, ensure_ascii=False, default=str),
            normalized_status,
            now.isoformat(),
            now.isoformat(),
            expires.isoformat(),
        ),
    )
    try:
        conn.commit()
    except sqlite3.OperationalError:
        # Outside explicit transaction / autocommit mode
        pass
    return session_id


def get_session(conn: sqlite3.Connection, session_id: str) -> SpecialistSession | None:
    row = conn.execute(
        """
        SELECT session_id, operator_id, thread_id, skill_id, specialist_type,
               project_id, parent_session_id, messages_json, tool_history_json,
               checkpoint_json, checkpoint_seq, status, created_at, updated_at,
               expires_at
        FROM specialist_sessions WHERE session_id = ?
        """,
        (session_id.strip(),),
    ).fetchone()
    if not row:
        return None
    data = dict(row)
    try:
        messages = json.loads(data["messages_json"] or "[]")
    except json.JSONDecodeError:
        messages = []
    try:
        tool_history = json.loads(data.get("tool_history_json") or "[]")
    except json.JSONDecodeError:
        tool_history = []
    try:
        checkpoint = json.loads(data.get("checkpoint_json") or "{}")
    except json.JSONDecodeError:
        checkpoint = {}
    return SpecialistSession(
        session_id=data["session_id"],
        operator_id=data["operator_id"],
        thread_id=data["thread_id"],
        skill_id=data["skill_id"],
        specialist_type=data["specialist_type"],
        project_id=data.get("project_id"),
        parent_session_id=data.get("parent_session_id"),
        messages=messages if isinstance(messages, list) else [],
        tool_history=tool_history if isinstance(tool_history, list) else [],
        checkpoint=checkpoint if isinstance(checkpoint, dict) else {},
        checkpoint_seq=int(data.get("checkpoint_seq") or 0),
        status=data["status"],
        created_at=data["created_at"],
        updated_at=data.get("updated_at") or data["created_at"],
        expires_at=data["expires_at"],
    )


def load_for_resume(
    conn: sqlite3.Connection,
    resume_from: str,
    *,
    to_skill_id: str,
    operator_id: str,
    thread_id: str,
    project_id: str | None = None,
) -> SpecialistSession:
    """Load and validate a session for resume_from. Raises ResumeError on reject."""
    sid = (resume_from or "").strip()
    if not sid:
        raise ResumeError("missing_session", "resume_from is empty")

    session = get_session(conn, sid)
    if session is None:
        raise ResumeError("not_found", f"specialist session '{sid}' not found")

    now = datetime.now(timezone.utc)
    try:
        exp = datetime.fromisoformat(session.expires_at.replace("Z", "+00:00"))
        if exp.tzinfo is None:
            exp = exp.replace(tzinfo=timezone.utc)
        if now > exp:
            raise ResumeError("expired", f"specialist session '{sid}' has expired")
    except ResumeError:
        raise
    except (TypeError, ValueError):
        logger.warning("Invalid expires_at on session %s", sid)

    op = operator_id or "default_operator"
    if session.operator_id != op:
        raise ResumeError("operator_mismatch", "session belongs to a different operator")

    th = thread_id or "default"
    if session.thread_id != th:
        raise ResumeError("thread_mismatch", "session belongs to a different thread")

    if project_id and session.project_id and project_id != session.project_id:
        raise ResumeError(
            "project_mismatch",
            f"session project_id={session.project_id} != request project_id={project_id}",
        )

    if not can_resume(session.skill_id, to_skill_id):
        raise ResumeError(
            "skill_not_allowed",
            f"cannot resume skill '{session.skill_id}' → '{to_skill_id}' "
            f"(same skill or allowlist only)",
        )

    return session


def build_resume_messages(
    prior: SpecialistSession,
    *,
    new_task: str,
    skill_system_prompt: str,
    project_id: str | None = None,
) -> list[dict[str, Any]]:
    """
    Rebuild messages for a resumed specialist loop.

    Best practice (Grok-style): re-render system prompt for the *target* skill;
    keep prior non-system transcript so tool evidence survives.
    """
    system = (skill_system_prompt or "").strip()
    if project_id:
        system = f"{system}\n\nProject scope: {project_id}".strip()
    system = (
        f"{system}\n\n"
        f"<resume-session>\n"
        f"Continuing specialist session {prior.session_id} "
        f"(prior skill={prior.skill_id}). "
        f"Prior tool results remain in the transcript. "
        f"Use only tools available in this run.\n"
        f"</resume-session>"
    ).strip()

    evidence = _checkpoint_evidence(prior.checkpoint)
    if evidence:
        evidence_json = json.dumps(evidence, ensure_ascii=False, separators=(",", ":"))
        system = (
            f"{system}\n\n"
            "<resume-checkpoint-evidence>\n"
            f"{evidence_json}\n"
            "</resume-checkpoint-evidence>\n"
            "These are bounded authoritative receipts recovered from the prior "
            "checkpoint. Prefer them over process narrative and do not re-fetch "
            "identical evidence unless freshness is required."
        )

    history = [m for m in prior.messages if m.get("role") != "system"]
    messages: list[dict[str, Any]] = [{"role": "system", "content": system}]
    messages.extend(history)
    messages.append({
        "role": "user",
        "content": (
            f"<resume-task>\n{new_task.strip()}\n</resume-task>\n"
            "Continue from the prior specialist work above. "
            "Do not re-fetch evidence already present unless needed."
        ),
    })
    return messages


def _checkpoint_evidence(checkpoint: dict[str, Any]) -> list[dict[str, Any]]:
    """Extract bounded receipt evidence for a resumed reasoning context."""
    raw_receipts = checkpoint.get("execution_receipts")
    if not isinstance(raw_receipts, list):
        return []
    evidence: list[dict[str, Any]] = []
    pending = list(raw_receipts)
    while pending and len(evidence) < _MAX_CHECKPOINT_RECEIPTS:
        raw = pending.pop(0)
        if not isinstance(raw, dict):
            continue
        children = raw.get("child_receipts")
        if isinstance(children, list):
            pending.extend(children)
        if str(raw.get("status") or "") != "succeeded":
            continue
        for item in raw.get("evidence") or []:
            if not isinstance(item, dict):
                continue
            locator = str(item.get("locator") or "").strip()
            if not locator:
                continue
            excerpt = str(item.get("excerpt") or "").strip()
            evidence.append({
                "source_type": str(item.get("source_type") or "reported"),
                "locator": locator,
                "authority": str(item.get("authority") or "reported"),
                "status": item.get("status"),
                "content_hash": item.get("content_hash"),
                "claim_coverage": list(item.get("claim_coverage") or []),
                "excerpt": excerpt[:_MAX_CHECKPOINT_EXCERPT_CHARS] or None,
            })
            if len(evidence) >= _MAX_CHECKPOINT_RECEIPTS:
                break
    return evidence
