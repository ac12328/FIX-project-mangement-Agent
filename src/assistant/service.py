"""Validated execution of AI Workspace turns into typed message parts."""

from __future__ import annotations

import json
import logging
import re
import sqlite3
from dataclasses import dataclass
from typing import Any, Callable
from urllib.parse import quote

from src.assistant.contracts import AssistantIntent
from src.assistant.receipt_hooks import WorkspaceReceiptHooks
from src.assistant.commands import AGENT_COMMANDS, UI_COMMANDS, parse_slash_command
from src.db.connection import run_external_io, scoped_write
from src.email.draft_session import create_draft, get_draft
from src.governance.action_service import create_action_intent
from src.repositories.action_intent_repo import get_action_intent_by_idempotency_key
from src.llm.client import llm_call_structured
from src.assistant.agent_runtime import run_workspace_agent

logger = logging.getLogger(__name__)


@dataclass
class ExecutionResult:
    text: str
    parts: list[dict[str, Any]]


_WRITABLE_ALIASES = {
    "status": "project_status",
    "project_status": "project_status",
    "ball": "ball_with",
    "ball_with": "ball_with",
    "milestone": "current_milestone",
    "current_milestone": "current_milestone",
    "next_action": "next_action",
    "target_go_live": "target_go_live",
    "notes": "notes",
    "market": "market",
}
_UPDATE_RE = re.compile(
    r"^(?:update|set|change)\s+(?:project\s+)?(?P<project>[^\s]+)\s+"
    r"(?P<field>project_status|status|ball_with|ball|current_milestone|milestone|"
    r"next_action|target_go_live|notes|market)\s+(?:to|=)\s+(?P<value>.+)$",
    re.IGNORECASE,
)

def turn_requires_write(
    message: str,
    intent: AssistantIntent | None,
    *,
    scope_mode: str = "portfolio",
    project_ids: list[str] | None = None,
) -> bool:
    """Return whether execution needs the serialized writer connection."""
    from src.services.operator_project_update import classify_workspace_operator_write

    if classify_workspace_operator_write(
        message,
        scope_mode=scope_mode,
        project_ids=list(project_ids or []),
        intent=intent,
    ):
        return True
    if intent and intent.type in {"draft", "project_update"}:
        return True
    command = parse_slash_command(message)
    if bool(command and command.name == "draft") or bool(_UPDATE_RE.match(message.strip())):
        return True
    from src.project_reconciliation.surface import classify_reconciliation_request

    # Preview also persists a durable checkpoint/proposal, so it is a write-class
    # turn even though it never mutates the project row.
    return classify_reconciliation_request(message) is not None


def execute_assistant_turn(
    conn: sqlite3.Connection,
    *,
    owner_user_id: str,
    can_create_actions: bool,
    thread_id: str,
    turn: dict[str, Any],
    message: str,
    intent: AssistantIntent | None = None,
    progress_cb=None,
    own_draft_write: bool = False,
    post_commit: list[Callable[[], None]] | None = None,
    receipt_hooks: WorkspaceReceiptHooks | None = None,
) -> ExecutionResult:
    """Execute one validated turn using the immutable scope/Web snapshot.

    ``own_draft_write`` is for a read-scoped caller that has released any
    writer lease before model generation; direct callers keep their legacy
    connection-owned draft commit behavior.
    """
    scope_mode = str(turn["scope_mode_snapshot"])
    project_ids = _snapshot_project_ids(turn)
    web_enabled = bool(turn["web_enabled_snapshot"])
    turn_id_value = turn.get("turn_id")
    turn_id = str(turn_id_value) if turn_id_value else None
    _validate_projects(conn, project_ids)

    command = parse_slash_command(message)
    if command and command.name in UI_COMMANDS:
        raise ValueError(f"/{command.name} is handled by the Workspace interface, not an agent turn")
    if command and command.name not in AGENT_COMMANDS:
        raise ValueError(f"Unknown command: /{command.name}")

    resolved_type = intent.type if intent else (command.name if command else None)
    if intent and command and intent.type != command.name:
        raise ValueError("Structured intent does not match the slash command")

    from src.project_reconciliation.surface import classify_reconciliation_request
    from src.services.operator_project_update import classify_workspace_operator_write

    write_intent = classify_workspace_operator_write(
        message,
        scope_mode=scope_mode,
        project_ids=project_ids,
        intent=intent,
    )
    if write_intent is not None:
        return _operator_update_result(
            conn,
            owner_user_id=owner_user_id,
            can_create_actions=can_create_actions,
            thread_id=thread_id,
            scope_mode=scope_mode,
            project_ids=project_ids,
            write_intent=write_intent,
            post_commit=post_commit,
        )

    reconciliation_request = classify_reconciliation_request(
        message,
        fallback_project_ids=(project_ids if len(project_ids) == 1 else ()),
    )
    if reconciliation_request is not None and not can_create_actions:
        text = (
            "你的角色可以分析專案資料，但沒有權限建立 reconciliation checkpoint "
            "或更新專案。"
        )
        return ExecutionResult(text=text, parts=[
            {"type": "text", "text": text},
            {"type": "error", "code": "read_only_execution", "message": text},
        ])

    if resolved_type == "status":
        return _general_result(
            conn, owner_user_id=owner_user_id, thread_id=thread_id, turn_id=turn_id, message=message,
            project_ids=project_ids, scope_mode=scope_mode, web_enabled=web_enabled,
            progress_cb=progress_cb, can_write=can_create_actions,
            post_commit=post_commit,
            receipt_hooks=receipt_hooks,
        )
    if resolved_type == "compare":
        selected = _intent_projects(intent, project_ids)
        if len(selected) < 2:
            raise ValueError("/compare requires at least two projects in the active context")
        _validate_projects(conn, selected)
        return _general_result(
            conn, owner_user_id=owner_user_id, thread_id=thread_id, turn_id=turn_id, message=message,
            project_ids=selected, scope_mode="projects", web_enabled=web_enabled,
            progress_cb=progress_cb, can_write=can_create_actions,
            post_commit=post_commit,
            receipt_hooks=receipt_hooks,
        )
    if resolved_type == "draft":
        if not can_create_actions:
            text = "Your role can analyze project data but cannot create drafts or business actions."
            return ExecutionResult(text=text, parts=[
                {"type": "text", "text": text},
                {"type": "error", "code": "read_only_execution", "message": text},
            ])
        selected = _intent_projects(intent, project_ids)
        _validate_projects(conn, selected)
        prompt = (intent.prompt if intent and intent.prompt else (command.arguments if command else message)).strip()
        return _draft_result(
            conn,
            owner_user_id=owner_user_id,
            project_ids=selected,
            prompt=prompt,
            own_write=own_draft_write,
        )
    if resolved_type == "aggregate_query":
        query = (intent.prompt if intent and intent.prompt else message).strip()
        return _general_result(
            conn, owner_user_id=owner_user_id, thread_id=thread_id, turn_id=turn_id, message=query,
            project_ids=project_ids, scope_mode=scope_mode, web_enabled=web_enabled,
            progress_cb=progress_cb, can_write=can_create_actions,
            post_commit=post_commit,
            receipt_hooks=receipt_hooks,
        )
    if resolved_type == "project_update":
        return _project_update_result(
            conn,
            owner_user_id=owner_user_id,
            can_create_actions=can_create_actions,
            project_ids=project_ids,
            intent=intent,
            message=message,
        )

    update_match = _UPDATE_RE.match(message.strip())
    if update_match:
        return _project_update_result(
            conn,
            owner_user_id=owner_user_id,
            can_create_actions=can_create_actions,
            project_ids=project_ids,
            intent=None,
            message=message,
        )
    return _general_result(
        conn,
        owner_user_id=owner_user_id,
        thread_id=thread_id,
        turn_id=turn_id,
        message=message,
        project_ids=project_ids,
        scope_mode=scope_mode,
        web_enabled=web_enabled,
        progress_cb=progress_cb,
        can_write=can_create_actions,
        post_commit=post_commit,
        receipt_hooks=receipt_hooks,
    )


def _snapshot_project_ids(turn: dict[str, Any]) -> list[str]:
    try:
        values = json.loads(turn.get("project_ids_snapshot_json") or "[]")
    except json.JSONDecodeError:
        values = []
    return list(dict.fromkeys(str(item) for item in values if str(item).strip()))


def _intent_projects(intent: AssistantIntent | None, snapshot: list[str]) -> list[str]:
    selected = list(dict.fromkeys(intent.project_ids)) if intent and intent.project_ids else list(snapshot)
    if snapshot and any(project_id not in snapshot for project_id in selected):
        raise ValueError("Intent projects must be part of the active thread context")
    return selected


def _validate_projects(conn: sqlite3.Connection, project_ids: list[str]) -> None:
    if not project_ids:
        return
    placeholders = ",".join("?" for _ in project_ids)
    rows = conn.execute(
        f"SELECT project_id FROM projects WHERE project_id IN ({placeholders})",
        project_ids,
    ).fetchall()
    found = {str(row[0]) for row in rows}
    missing = [project_id for project_id in project_ids if project_id not in found]
    if missing:
        raise ValueError(f"Project not found: {', '.join(missing)}")


def _project_rows(conn: sqlite3.Connection, project_ids: list[str] | None = None) -> list[dict[str, Any]]:
    params: list[Any] = []
    where = "WHERE project_id != '_system'"
    if project_ids:
        where += f" AND project_id IN ({','.join('?' for _ in project_ids)})"
        params.extend(project_ids)
    rows = conn.execute(
        f"""SELECT project_id, client_name, market, project_status, current_milestone,
                   ball_with, next_action, target_go_live, client_email, updated_at
            FROM projects {where}
            ORDER BY CASE project_status WHEN 'Blocked' THEN 0 WHEN 'Waiting' THEN 1 ELSE 2 END,
                     updated_at DESC, project_id""",
        params,
    ).fetchall()
    return [dict(row) for row in rows]


def _draft_result(
    conn: sqlite3.Connection,
    *,
    owner_user_id: str,
    project_ids: list[str],
    prompt: str,
    own_write: bool = False,
) -> ExecutionResult:
    project_id = project_ids[0] if project_ids else None
    project = _project_rows(conn, [project_id])[0] if project_id else None
    prompt = prompt or "Draft a concise project update email."
    subject, body = _generate_draft(
        conn, prompt=prompt, project=project, owner_user_id=owner_user_id,
        writer_owned=not own_write,
    )

    def persist(wconn, *, commit=True, post_commit=None):
        draft_id = create_draft(
            wconn,
            project_id,
            "ai_workspace",
            subject,
            body,
            to_email=project.get("client_email") if project else None,
            context_packet_ref=f"assistant:{owner_user_id}",
            commit=commit,
            post_commit=post_commit,
        )
        return draft_id, get_draft(wconn, draft_id) or {}

    if own_write:
        effects = []
        with scoped_write() as wconn:
            # The scoped read used for model context is no longer authoritative.
            # Reject a stale draft rather than targeting an old client address.
            if project_id and _project_rows(wconn, [project_id]) != [project]:
                raise ValueError("Project changed during draft generation; please retry")
            draft_id, draft = persist(wconn, commit=False, post_commit=effects)
        for effect in effects:
            try:
                effect()
            except Exception:
                logger.exception("Committed Workspace draft post-commit effect failed: %s", draft_id)
    else:
        # Direct callers retain the existing caller-connection/commit contract.
        draft_id, draft = persist(conn)
    text = "Draft created in Draft Workspace. It has not been sent."
    return ExecutionResult(text=text, parts=[
        {"type": "text", "text": text},
        {
            "type": "draft",
            "draft_id": draft_id,
            "project_id": project_id,
            "subject": draft.get("subject") or subject,
            "body": draft.get("current_draft_text") or body,
            "to_email": draft.get("to_email"),
            "status": draft.get("status") or "created",
            "url": f"/drafts/{quote(draft_id)}",
        },
    ])


def _generate_draft(
    conn: sqlite3.Connection,
    *,
    prompt: str,
    project: dict[str, Any] | None,
    owner_user_id: str,
    writer_owned: bool = True,
) -> tuple[str, str]:
    project_context = json.dumps(project or {}, ensure_ascii=False, default=str)
    messages = [
        {
            "role": "system",
            "content": (
                "Create an email draft only. Never claim it was sent. Return a concise subject and body. "
                "Use placeholders in square brackets when recipient details are unknown."
            ),
        },
        {"role": "user", "content": f"Project: {project_context}\nRequest: {prompt}"},
    ]
    try:
        def call_model():
            return llm_call_structured(
                "draft",
                messages,
                {
                    "type": "object",
                    "properties": {"subject": {"type": "string"}, "body": {"type": "string"}},
                    "required": ["subject", "body"],
                },
                temperature=0.2,
                timeout_sec=45,
            )

        result = run_external_io(conn, call_model) if writer_owned else call_model()
        subject = str(result.get("subject") or "Project update").strip()[:240]
        body = str(result.get("body") or "").strip()
        if len(body) >= 20:
            return subject, body
    except Exception:
        pass
    client = str((project or {}).get("client_name") or "there")
    subject = f"Project update - {client}"[:240]
    body = (
        f"Hello {client},\n\n"
        f"{prompt.strip()}\n\n"
        "Please let us know if you have any questions or if any details need to be adjusted.\n\n"
        "Best regards,\n[Your name]"
    )
    return subject, body


def _operator_update_result(
    conn: sqlite3.Connection,
    *,
    owner_user_id: str,
    can_create_actions: bool,
    thread_id: str,
    scope_mode: str,
    project_ids: list[str],
    write_intent,
    post_commit: list[Callable[[], None]] | None = None,
) -> ExecutionResult:
    from config import defaults
    from src.services.operator_project_update import (
        OperatorUpdateError,
        is_usable_override_reason,
        preview_operator_update,
        stage_operator_update,
    )

    if not can_create_actions:
        text = "Your role can analyze project data but cannot change project truth."
        return ExecutionResult(text=text, parts=[
            {"type": "text", "text": text},
            {"type": "error", "code": "read_only_execution", "message": text},
        ])
    if (
        scope_mode == "projects"
        and project_ids
        and write_intent.project_id not in project_ids
    ):
        text = f"{write_intent.project_id} is outside the current Workspace scope."
        return ExecutionResult(text=text, parts=[
            {"type": "text", "text": text},
            {"type": "error", "code": "project_outside_workspace_scope", "message": text},
        ])
    if not bool(getattr(defaults, "WORKSPACE_OPERATOR_WRITES_ENABLED", False)):
        text = (
            "Workspace operator writes are disabled. "
            "Use Telegram or set ai_workspace.operator_writes_enabled."
        )
        return ExecutionResult(text=text, parts=[{"type": "text", "text": text}])

    fields = {"current_milestone": write_intent.to_milestone}
    if write_intent.next_action:
        fields["next_action"] = write_intent.next_action
    if not is_usable_override_reason(write_intent.reason_text):
        text = (
            f"Proposed {write_intent.project_id} → {write_intent.to_milestone}. "
            "Add a specific reason (20+ characters) to stage the override."
        )
        return ExecutionResult(text=text, parts=[
            {"type": "text", "text": text},
            {
                "type": "operator_update",
                "status": "needs_intake",
                "project_id": write_intent.project_id,
                "fields": fields,
                "reason": write_intent.reason_text,
            },
        ])
    try:
        proposal = preview_operator_update(
            conn,
            project_id=write_intent.project_id,
            fields=fields,
            reason=write_intent.reason_text,
        )
        operation = stage_operator_update(
            conn,
            actor=owner_user_id,
            thread_id=thread_id,
            proposal=proposal,
            reason=write_intent.reason_text,
            evidence_ref=None,
            post_commit=post_commit,
        )
    except OperatorUpdateError as exc:
        return ExecutionResult(text=exc.message, parts=[
            {"type": "text", "text": exc.message},
            {"type": "error", "code": exc.code, "message": exc.message},
        ])
    text = (
        f"Staged {write_intent.project_id} milestone change for confirmation. "
        "Nothing has been written yet."
    )
    return ExecutionResult(text=text, parts=[
        {"type": "text", "text": text},
        {
            "type": "operator_update",
            "status": "pending",
            "operation_id": operation.operation_id,
            "project_id": write_intent.project_id,
            "kind": proposal.kind,
            "fields": proposal.fields,
            "missing_criteria": proposal.missing_criteria,
            "reason": operation.payload.get("reason"),
            "transition": proposal.transition,
            "base_version": operation.base_version,
            "expires_at": operation.expires_at,
        },
    ])


def _project_update_result(
    conn: sqlite3.Connection,
    *,
    owner_user_id: str,
    can_create_actions: bool,
    project_ids: list[str],
    intent: AssistantIntent | None,
    message: str,
) -> ExecutionResult:
    if not can_create_actions:
        text = "Your role can analyze project data but cannot create business action intents."
        return ExecutionResult(text=text, parts=[
            {"type": "text", "text": text},
            {"type": "error", "code": "read_only_execution", "message": text},
        ])
    if intent:
        project_id = intent.project_ids[0] if intent.project_ids else (project_ids[0] if len(project_ids) == 1 else "")
        field = _WRITABLE_ALIASES.get(str(intent.field or "").lower())
        value = intent.value
    else:
        match = _UPDATE_RE.match(message.strip())
        if not match:
            raise ValueError("Project update intent is incomplete")
        project_id = match.group("project")
        field = _WRITABLE_ALIASES.get(match.group("field").lower())
        value = match.group("value").strip()
    if not project_id or not field:
        raise ValueError("Project update requires one project, field, and value")
    if field == "current_milestone":
        text = (
            "Milestone changes must use the Workspace operator-update card, "
            "not an Approvals action intent."
        )
        return ExecutionResult(text=text, parts=[
            {"type": "text", "text": text},
            {"type": "error", "code": "field_not_in_v1", "message": text},
        ])
    logger.warning(
        "assistant_project_update_skeleton field=%s project_id=%s",
        field, project_id,
    )
    if project_ids and project_id not in project_ids:
        raise ValueError("Project update target must be part of the active context")
    _validate_projects(conn, [project_id])
    current = conn.execute(f"SELECT {field} FROM projects WHERE project_id=?", (project_id,)).fetchone()
    old_value = current[0] if current else None
    payload = {
        "project_id": project_id,
        "fields": {field: value},
        "reason": "Requested from AI Workspace",
        "evidence_ref": None,
    }
    idempotency_key = f"assistant-update:{owner_user_id}:{project_id}:{field}:{value}"
    action = get_action_intent_by_idempotency_key(conn, idempotency_key)
    if action is None:
        try:
            action = create_action_intent(
                conn,
                operator_id=owner_user_id,
                action_type="assistant_project_update",
                risk_class="medium" if field in {"project_status", "current_milestone"} else "low",
                target_type="project",
                target_id=project_id,
                payload=payload,
                created_by_surface="ai_workspace",
                idempotency_key=idempotency_key,
            )
        except sqlite3.IntegrityError:
            action = get_action_intent_by_idempotency_key(conn, idempotency_key)
            if action is None:
                raise
    text = "A project update is ready for review. Nothing has been changed yet."
    return ExecutionResult(text=text, parts=[
        {"type": "text", "text": text},
        {
            "type": "action",
            "action_id": action.action_id,
            "action_type": action.action_type,
            "status": action.status,
            "risk": action.risk_class,
            "target_type": "project",
            "target_id": project_id,
            "diff": [{"field": field, "before": old_value, "after": value}],
            "expires_at": action.expires_at,
            "approve_url": f"/api/approvals/{action.action_id}/approve?execute=true",
            "reject_url": f"/api/approvals/{action.action_id}/reject",
        },
    ])


def _general_result(
    conn: sqlite3.Connection,
    *,
    owner_user_id: str,
    thread_id: str,
    turn_id: str | None = None,
    message: str,
    project_ids: list[str],
    scope_mode: str,
    web_enabled: bool,
    progress_cb=None,
    can_write: bool = False,
    post_commit: list[Callable[[], None]] | None = None,
    receipt_hooks: WorkspaceReceiptHooks | None = None,
) -> ExecutionResult:
    effect_start = len(post_commit) if post_commit is not None else 0
    result = run_workspace_agent(
        conn,
        owner_user_id=owner_user_id,
        thread_id=thread_id,
        turn_id=turn_id,
        message=message,
        project_ids=project_ids,
        scope_mode=scope_mode,
        web_enabled=web_enabled,
        progress_cb=progress_cb,
        can_write=can_write,
        post_commit=post_commit,
        receipt_hooks=receipt_hooks,
    )
    response = ExecutionResult(text=result.text, parts=result.parts)
    if (result.tool_results
            and result.tool_results[0].get("result", {}).get("status") == "pending_commit"):
        def refresh_confirmed_response() -> None:
            response.text = result.text
            response.parts = result.parts

        if receipt_hooks is not None:
            receipt_hooks.refresh_response = refresh_confirmed_response
        elif post_commit is not None and len(post_commit) > effect_start:
            # Direct list-based callers retain their existing hydration behavior.
            post_commit.insert(effect_start + 1, refresh_confirmed_response)
    return response
