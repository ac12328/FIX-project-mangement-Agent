"""Owned thread and streaming turn APIs for the AI Workspace."""

from __future__ import annotations

import hashlib
import json
import logging
import queue
import re
import sqlite3
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import StreamingResponse
from opentelemetry import context as otel_context, trace
from opentelemetry.trace import Status, StatusCode

from config import defaults
from src.api.auth import CurrentActor
from src.api.deps import get_current_actor, get_read_conn, get_write_actor, get_write_conn
from src.api.schemas.assistant import (
    AssistantCapabilitiesResponse,
    AssistantMessagePage,
    AssistantThreadCreate,
    AssistantThreadPatch,
    AssistantThreadPage,
    AssistantThreadResponse,
    AssistantTurnRequest,
)
from src.api.schemas.assistant_stream import validate_assistant_stream_event
from src.assistant.commands import ALL_COMMANDS, parse_slash_command
from src.assistant.follow_up_questions import generate_follow_up_questions
from src.assistant.reconciliation_replay import committed_receipt_for_turn
from src.assistant.receipt_hooks import WorkspaceReceiptHooks
from src.assistant.service import (
    ExecutionResult,
    _snapshot_project_ids,
    execute_assistant_turn,
    turn_requires_write,
)
from src.assistant.web_search import web_search_capability
from src.auth.rbac import P_BUSINESS_WRITE
from src.db.connection import scoped_read, scoped_write
from src.repositories.assistant_repo import (
    AssistantThreadHasRunningTurnError,
    create_thread,
    create_turn,
    delete_thread as delete_thread_record,
    get_thread,
    get_turn,
    get_turn_messages,
    list_messages,
    list_threads,
    persist_turn_terminal_message,
    save_message,
    update_thread,
)

router = APIRouter(prefix="/assistant", tags=["assistant"])
logger = logging.getLogger(__name__)

_COMMAND_DESCRIPTIONS = {
    "new": "Start a new thread.",
    "resume": "Search and resume a saved thread.",
    "project": "Add one or more project contexts.",
    "portfolio": "Switch to the full portfolio.",
    "status": "Analyze the current scope.",
    "compare": "Compare at least two selected projects.",
    "draft": "Create an email draft without sending it.",
    "web": "Turn external search on or off when configured.",
    "help": "Show commands, capabilities, and permissions.",
}
_PART_TYPES = [
    "text", "table", "source", "tool_result", "draft", "action",
    "follow_up_questions", "error", "operator_update",
]

_HEARTBEAT_SECONDS = 5.0
_ACTIVE_TURNS: dict[str, threading.Thread] = {}
_ACTIVE_TURNS_LOCK = threading.Lock()


def _register_active_turn(turn_id: str, worker: threading.Thread) -> None:
    with _ACTIVE_TURNS_LOCK:
        _ACTIVE_TURNS[turn_id] = worker


def _unregister_active_turn(turn_id: str) -> None:
    with _ACTIVE_TURNS_LOCK:
        _ACTIVE_TURNS.pop(turn_id, None)


def _active_turn(turn_id: str) -> threading.Thread | None:
    with _ACTIVE_TURNS_LOCK:
        worker = _ACTIVE_TURNS.get(turn_id)
        if worker is not None and not worker.is_alive():
            _ACTIVE_TURNS.pop(turn_id, None)
            return None
        return worker


def _require_enabled() -> None:
    if not getattr(defaults, "AI_WORKSPACE_ENABLED", False):
        raise HTTPException(404, "AI Workspace is disabled")


def _validate_projects(conn: sqlite3.Connection, project_ids: list[str]) -> list[str]:
    normalized = list(dict.fromkeys(item.strip() for item in project_ids if item.strip()))
    if not normalized:
        return []
    placeholders = ",".join("?" for _ in normalized)
    rows = conn.execute(
        f"SELECT project_id FROM projects WHERE project_id IN ({placeholders}) AND project_id != '_system'",
        normalized,
    ).fetchall()
    found = {str(row[0]) for row in rows}
    missing = [project_id for project_id in normalized if project_id not in found]
    if missing:
        raise HTTPException(404, f"Project not found: {', '.join(missing)}")
    return normalized


def _validate_web_enabled(web_enabled: bool) -> None:
    if not web_enabled:
        return
    available, reason, _ = web_search_capability()
    if not available:
        raise HTTPException(409, reason or "Web Search is unavailable")


@router.get("/capabilities", response_model=AssistantCapabilitiesResponse)
def capabilities(actor: CurrentActor = Depends(get_current_actor)) -> AssistantCapabilitiesResponse:
    available, reason, provider = web_search_capability()
    return AssistantCapabilitiesResponse(
        enabled=bool(getattr(defaults, "AI_WORKSPACE_ENABLED", False)),
        web_search_available=available,
        web_search_reason=reason,
        web_search_provider=provider,
        commands=[
            {"name": name, "usage": f"/{name}", "description": _COMMAND_DESCRIPTIONS[name]}
            for name in sorted(ALL_COMMANDS, key=lambda item: list(_COMMAND_DESCRIPTIONS).index(item))
        ],
        part_types=_PART_TYPES,
        can_create_actions=P_BUSINESS_WRITE in actor.permissions,
        operator_writes=bool(getattr(defaults, "WORKSPACE_OPERATOR_WRITES_ENABLED", False)),
    )


@router.get("/threads", response_model=AssistantThreadPage)
def get_threads(
    search: str | None = None,
    archived: bool = False,
    project_id: str | None = None,
    limit: int = Query(30, ge=1, le=100),
    cursor: str | None = None,
    actor: CurrentActor = Depends(get_current_actor),
    conn: sqlite3.Connection = Depends(get_read_conn),
) -> dict[str, Any]:
    _require_enabled()
    try:
        page = list_threads(
            conn,
            owner_user_id=actor.user_id,
            search=search,
            archived=archived,
            project_id=project_id,
            limit=limit,
            cursor=cursor,
        )
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    return {"items": page.items, "next_cursor": page.next_cursor}


@router.post("/threads", status_code=201, response_model=AssistantThreadResponse)
def post_thread(
    body: AssistantThreadCreate,
    actor: CurrentActor = Depends(get_write_actor),
) -> dict[str, Any]:
    _require_enabled()
    _validate_web_enabled(body.web_enabled)
    with scoped_write() as conn:
        project_ids = _validate_projects(conn, body.project_ids)
        result = create_thread(
            conn,
            owner_user_id=actor.user_id,
            title=body.title,
            scope_mode=body.scope_mode,
            project_ids=project_ids,
            web_enabled=body.web_enabled,
        )
    return result


@router.get("/threads/{thread_id}", response_model=AssistantThreadResponse)
def get_thread_detail(
    thread_id: str,
    actor: CurrentActor = Depends(get_current_actor),
    conn: sqlite3.Connection = Depends(get_read_conn),
) -> dict[str, Any]:
    _require_enabled()
    thread = get_thread(conn, thread_id, owner_user_id=actor.user_id)
    if thread is None:
        raise HTTPException(404, "Thread not found")
    return thread


@router.patch("/threads/{thread_id}", response_model=AssistantThreadResponse)
def patch_thread(
    thread_id: str,
    body: AssistantThreadPatch,
    actor: CurrentActor = Depends(get_write_actor),
    conn: sqlite3.Connection = Depends(get_write_conn),
) -> dict[str, Any]:
    _require_enabled()
    current = get_thread(conn, thread_id, owner_user_id=actor.user_id)
    if current is None:
        raise HTTPException(404, "Thread not found")
    values = body.model_dump(exclude_unset=True)
    scope_mode = values.get("scope_mode", current["scope_mode"])
    project_ids = values.get("project_ids", current["project_ids"])
    if scope_mode == "portfolio":
        project_ids = []
    elif not project_ids:
        raise HTTPException(422, "Project scope requires at least one project")
    project_ids = _validate_projects(conn, project_ids)
    web_enabled = values.get("web_enabled", current["web_enabled"])
    _validate_web_enabled(bool(web_enabled))
    updated = update_thread(
        conn,
        thread_id,
        owner_user_id=actor.user_id,
        title=values.get("title"),
        scope_mode=scope_mode if "scope_mode" in values else None,
        project_ids=project_ids if ("scope_mode" in values or "project_ids" in values) else None,
        web_enabled=values.get("web_enabled"),
        archived=values.get("archived"),
    )
    if updated is None:
        raise HTTPException(404, "Thread not found")
    if "scope_mode" in values or "project_ids" in values:
        previous = current.get("project_ids", []) if current["scope_mode"] == "projects" else []
        visible_text = (
            "Context changed to the full portfolio."
            if scope_mode == "portfolio"
            else f"Context changed to projects: {', '.join(project_ids)}."
        )
        if current["scope_mode"] != scope_mode or previous != project_ids:
            save_message(
                conn,
                thread_id=thread_id,
                role="system",
                content=visible_text,
                parts=[{
                    "type": "tool_result",
                    "tool": "context_change",
                    "scope_mode": scope_mode,
                    "project_ids": project_ids,
                    "text": visible_text,
                }],
            )
            updated = get_thread(conn, thread_id, owner_user_id=actor.user_id) or updated
    return updated


@router.delete("/threads/{thread_id}")
def delete_thread(
    thread_id: str,
    actor: CurrentActor = Depends(get_write_actor),
    conn: sqlite3.Connection = Depends(get_write_conn),
) -> dict[str, Any]:
    _require_enabled()
    try:
        deleted = delete_thread_record(
            conn,
            thread_id,
            owner_user_id=actor.user_id,
        )
    except AssistantThreadHasRunningTurnError as exc:
        raise HTTPException(409, str(exc)) from exc
    if not deleted:
        raise HTTPException(404, "Thread not found")
    return {"deleted": True, "thread_id": thread_id}


@router.get("/threads/{thread_id}/messages", response_model=AssistantMessagePage)
def get_messages(
    thread_id: str,
    limit: int = Query(80, ge=1, le=200),
    cursor: str | None = None,
    actor: CurrentActor = Depends(get_current_actor),
    conn: sqlite3.Connection = Depends(get_read_conn),
) -> dict[str, Any]:
    _require_enabled()
    try:
        page = list_messages(
            conn,
            thread_id,
            owner_user_id=actor.user_id,
            limit=limit,
            cursor=cursor,
        )
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    if page is None:
        raise HTTPException(404, "Thread not found")
    return {"items": page.items, "next_cursor": page.next_cursor}


@router.post("/threads/{thread_id}/turns")
def post_turn(
    thread_id: str,
    body: AssistantTurnRequest,
    actor: CurrentActor = Depends(get_current_actor),
) -> StreamingResponse:
    _require_enabled()
    parsed = parse_slash_command(body.message)
    command = (body.command or (parsed.name if parsed else None))
    if body.command and parsed and body.command.lower().lstrip("/") != parsed.name:
        raise HTTPException(422, "Command does not match the message")
    command = command.lower().lstrip("/") if command else None
    if command and command not in ALL_COMMANDS:
        raise HTTPException(422, f"Unknown command: /{command}")

    with scoped_write() as conn:
        thread = get_thread(conn, thread_id, owner_user_id=actor.user_id)
        if thread is None:
            raise HTTPException(404, "Thread not found")
        if thread.get("archived_at"):
            raise HTTPException(409, "Archived threads cannot accept new turns")
        try:
            turn, created = create_turn(
                conn,
                thread=thread,
                client_turn_id=body.client_turn_id,
                user_text=body.message,
                command=command,
            )
        except sqlite3.IntegrityError:
            row = conn.execute(
                "SELECT * FROM assistant_turns WHERE thread_id=? AND client_turn_id=?",
                (thread_id, body.client_turn_id),
            ).fetchone()
            if row is None:
                raise
            turn, created = dict(row), False

    generator = _turn_event_stream(
        actor=actor,
        thread_id=thread_id,
        turn=turn,
        created=created,
        body=body,
    )
    return StreamingResponse(
        generator,
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache, no-transform",
            "X-Accel-Buffering": "no",
        },
    )


@contextmanager
def _detached_span(tracer: Any, name: str) -> Iterator[Any]:
    """End a span without keeping an OpenTelemetry context attached across SSE yields."""
    span = tracer.start_span(name)
    try:
        yield span
    finally:
        span.end()


def _turn_event_stream(
    *,
    actor: CurrentActor,
    thread_id: str,
    turn: dict[str, Any],
    created: bool,
    body: AssistantTurnRequest,
) -> Iterator[str]:
    turn_id = str(turn["turn_id"])
    event_queue: queue.Queue[tuple[str, Any]] = queue.Queue()
    worker: threading.Thread | None = None
    started_at = time.monotonic()
    last_visible_event_at = started_at
    first_progress_seen = False
    tracer = trace.get_tracer("agentfixbpro.assistant")
    with _detached_span(tracer, "assistant.turn") as span:
        span.set_attribute("assistant.surface", "web")
        span.set_attribute("assistant.turn_id", turn_id)
        span.set_attribute("assistant.thread_id", thread_id)
        span.set_attribute("assistant.replayed", not created)
        span.set_attribute("assistant.intent", str(body.intent or ""))
        span.set_attribute("assistant.request_chars", len(body.message))
        span.set_attribute(
            "assistant.request_hash",
            hashlib.sha256(body.message.encode("utf-8")).hexdigest(),
        )
        try:
            if created:
                parent_context = trace.set_span_in_context(span)
                worker = threading.Thread(
                    target=_execute_turn_worker,
                    kwargs={
                        "event_queue": event_queue,
                        "actor": actor,
                        "thread_id": thread_id,
                        "turn": turn,
                        "body": body,
                        "parent_context": parent_context,
                    },
                    name=f"assistant-turn-{turn_id[:12]}",
                    daemon=True,
                )
                _register_active_turn(turn_id, worker)
                try:
                    worker.start()
                except Exception:
                    _unregister_active_turn(turn_id)
                    raise

            yield _sse("turn.started", {"turn_id": turn_id, "replayed": not created})
            if not created:
                # A replay read is not an execution failure: a write may have committed.
                try:
                    yield from _replay_or_follow_running_turn(turn_id, turn)
                except Exception:
                    logger.exception("Could not read replay state for turn %s", turn_id)
                    yield _unknown_turn_progress(turn_id)
                span.set_attribute("assistant.status", "replayed")
                return

            accepted_ms = (time.monotonic() - started_at) * 1000
            span.add_event("accepted", {"elapsed_ms": round(accepted_ms, 3)})
            yield _sse("progress", {
                "turn_id": turn_id,
                "phase": "accepted",
                "message": "Request accepted",
            })
            last_visible_event_at = time.monotonic()

            terminal: dict[str, Any] | None = None
            while terminal is None:
                try:
                    event_type, payload = event_queue.get(timeout=0.25)
                except queue.Empty:
                    now = time.monotonic()
                    if now - last_visible_event_at >= _HEARTBEAT_SECONDS:
                        yield _sse("progress", {
                            "turn_id": turn_id,
                            "phase": "analysis",
                            "message": "Still working on verified Workspace records",
                        })
                        last_visible_event_at = now
                    if worker is not None and not worker.is_alive():
                        raise RuntimeError("The Workspace worker stopped without a terminal result")
                    continue
                if event_type == "progress":
                    if not first_progress_seen:
                        first_progress_seen = True
                        first_progress_ms = (time.monotonic() - started_at) * 1000
                        span.set_attribute("assistant.first_progress_ms", round(first_progress_ms, 3))
                        span.add_event("first_progress", {"elapsed_ms": round(first_progress_ms, 3)})
                    yield _sse("progress", {"turn_id": turn_id, **payload})
                    last_visible_event_at = time.monotonic()
                elif event_type == "completed":
                    terminal = dict(payload)
                elif event_type == "failed":
                    terminal = dict(payload)
                elif event_type == "unknown":
                    span.set_attribute("assistant.status", "unknown")
                    yield _unknown_turn_progress(turn_id)
                    return
                elif event_type == "result":
                    # Compatibility for tests or older in-process workers. Persist here once.
                    terminal = _persist_worker_success(
                        thread_id=thread_id,
                        turn_id=turn_id,
                        result=payload,
                    )
                elif event_type == "error":
                    terminal = _persist_worker_failure(
                        thread_id=thread_id,
                        turn_id=turn_id,
                        exc=payload,
                    )

            if terminal.get("status") == "failed":
                error = str(terminal.get("error") or "Turn failed")
                part = terminal.get("part") or {
                    "type": "error",
                    "code": "turn_failed",
                    "message": error,
                }
                span.set_attribute("assistant.status", "failed")
                span.set_status(Status(StatusCode.ERROR, error))
                yield _sse("part.added", {"turn_id": turn_id, "part": part})
                yield _sse("turn.failed", {"turn_id": turn_id, "error": error})
                return

            result = terminal["result"]
            yield _sse("progress", {
                "turn_id": turn_id,
                "phase": "finalizing",
                "message": "Preparing verified results",
            })
            if result.text:
                first_text_ms = (time.monotonic() - started_at) * 1000
                span.set_attribute("assistant.first_text_ms", round(first_text_ms, 3))
                span.add_event("first_text", {"elapsed_ms": round(first_text_ms, 3)})
                yield _sse("text.delta", {"turn_id": turn_id, "delta": result.text})
            for part in result.parts:
                if part.get("type") != "text":
                    yield _sse("part.added", {"turn_id": turn_id, "part": part})
            span.set_attribute("assistant.output_chars", len(result.text or ""))
            span.set_attribute("assistant.part_count", len(result.parts))
            span.set_attribute(
                "assistant.tool_count",
                len(getattr(result, "tool_names", ()) or ()),
            )
            completed_ms = (time.monotonic() - started_at) * 1000
            span.set_attribute("assistant.completed_ms", round(completed_ms, 3))
            span.set_attribute("assistant.status", "completed")
            span.add_event("completed", {"elapsed_ms": round(completed_ms, 3)})
            yield _sse("turn.completed", {
                "turn_id": turn_id,
                "message_id": terminal.get("message_id"),
            })
        except GeneratorExit:
            span.set_attribute("assistant.status", "disconnected")
            span.add_event(
                "disconnected",
                {"elapsed_ms": round((time.monotonic() - started_at) * 1000, 3)},
            )
            # The worker remains the owner of terminal persistence and may finish in background.
            raise
        except Exception as exc:
            span.record_exception(exc)
            span.set_status(Status(StatusCode.ERROR, type(exc).__name__))
            span.set_attribute("assistant.status", "failed")
            try:
                terminal = _persist_worker_failure(
                    thread_id=thread_id,
                    turn_id=turn_id,
                    exc=exc,
                )
            except Exception as persistence_exc:
                span.record_exception(persistence_exc)
                terminal = _failure_envelope(exc)
            error = str(terminal.get("error") or exc.__class__.__name__)
            part = terminal.get("part") or {"type": "error", "code": "turn_failed", "message": error}
            yield _sse("part.added", {"turn_id": turn_id, "part": part})
            yield _sse("turn.failed", {"turn_id": turn_id, "error": error})


def _persist_worker_success(*, thread_id: str, turn_id: str, result: Any) -> dict[str, Any]:
    parts = list(result.parts)
    for part in parts:
        if part.get("type") != "text":
            validate_assistant_stream_event("part.added", {"turn_id": turn_id, "part": part})
    with scoped_write() as conn:
        message = persist_turn_terminal_message(
            conn,
            thread_id=thread_id,
            turn_id=turn_id,
            status="completed",
            content=str(result.text or ""),
            parts=parts,
        )
    return {
        "status": "completed",
        "result": result,
        "message_id": message.get("message_id") if message else None,
    }


def _failure_envelope(exc: BaseException) -> dict[str, Any]:
    error = str(exc) or exc.__class__.__name__
    return {
        "status": "failed",
        "error": error,
        "part": {"type": "error", "code": "turn_failed", "message": error},
        "message_id": None,
    }


def _persist_worker_failure(*, thread_id: str, turn_id: str, exc: BaseException) -> dict[str, Any]:
    terminal = _failure_envelope(exc)
    with scoped_write() as conn:
        message = persist_turn_terminal_message(
            conn,
            thread_id=thread_id,
            turn_id=turn_id,
            status="failed",
            content=str(terminal["error"]),
            parts=[dict(terminal["part"])],
            error=str(terminal["error"]),
        )
    terminal["message_id"] = message.get("message_id") if message else None
    return terminal


def _pending_reconciliation_operation(result: ExecutionResult) -> str | None:
    """Identify the outer-commit receipt callback, not ordinary publication effects."""
    if not any(
        part.get("type") == "tool_result"
        and part.get("tool") == "project_reconcile"
        and part.get("status") == "running"
        and part.get("text") == "Awaiting outer DB commit"
        for part in result.parts
    ):
        return None
    match = re.search(r"Operation[\uFF1A:]\s*`([^`]+)`", result.text)
    return match.group(1) if match else "unavailable"


def _unknown_reconciliation_receipt(result: ExecutionResult, operation_id: str) -> None:
    """The writer committed, but we cannot claim the receipt was verified."""
    text = (
        "The outer DB writer committed, but reconciliation receipt verification failed. "
        "The project outcome is unknown (unknown verification), not a failed write. "
        f"Do not retry until you inspect Operation: `{operation_id}` and the project state."
    )
    result.text = text
    result.parts = [
        {"type": "text", "text": text},
        {
            "type": "tool_result", "tool": "project_reconcile", "status": "unknown",
            "text": f"Receipt verification unavailable; Operation: {operation_id}",
        },
    ]


def _warn_unpersisted_reply(result: ExecutionResult) -> None:
    """Retain the command outcome for the live client when message storage fails."""
    warning = (
        "\n\nThis reply could not be saved to Workspace conversation history. "
        "Check the Operation above or inspect the project before retrying; "
        "reply storage failure does not mean the project write failed."
    )
    result.text = str(result.text or "") + warning
    result.parts = [
        {"type": "text", "text": result.text},
        *(part for part in result.parts if part.get("type") != "text"),
    ]


def _execute_turn_worker(
    *,
    event_queue: queue.Queue[tuple[str, Any]],
    actor: CurrentActor,
    thread_id: str,
    turn: dict[str, Any],
    body: AssistantTurnRequest,
    parent_context: Any,
) -> None:
    turn_id = str(turn["turn_id"])
    token = otel_context.attach(parent_context)

    def progress(step_name: str, status: str, detail: str | None) -> None:
        event_queue.put(("progress", _coarse_progress(step_name, status, detail)))

    try:
        from src.services.operator_project_update import classify_workspace_operator_write

        scope_mode = str(turn.get("scope_mode_snapshot") or "portfolio")
        project_ids = _snapshot_project_ids(turn)
        command = parse_slash_command(body.message)
        is_draft = (
            body.intent.type == "draft" if body.intent is not None
            else bool(command and command.name == "draft")
        )
        # Operator updates take precedence over the draft branch in the service.
        # A structured draft intent must not accidentally write via a read scope.
        own_draft_write = is_draft and not classify_workspace_operator_write(
            body.message, scope_mode=scope_mode, project_ids=project_ids,
            intent=body.intent,
        )
        owns_write_scope = not own_draft_write and turn_requires_write(
            body.message, body.intent,
            scope_mode=scope_mode, project_ids=project_ids,
        )
        connection_scope = scoped_write if owns_write_scope else scoped_read
        post_commit_effects = [] if owns_write_scope else None
        receipt_hooks = WorkspaceReceiptHooks() if owns_write_scope else None
        with connection_scope() as conn:
            result = execute_assistant_turn(
                conn,
                owner_user_id=actor.user_id,
                can_create_actions=P_BUSINESS_WRITE in actor.permissions,
                thread_id=thread_id,
                turn=turn,
                message=body.message,
                intent=body.intent,
                progress_cb=progress,
                own_draft_write=own_draft_write,
                post_commit=post_commit_effects,
                receipt_hooks=receipt_hooks,
            )
        # The outer writer has exited successfully. Only named receipt hooks can
        # confirm the answer; every independent publication remains best-effort.
        effects = post_commit_effects or []
        verified_receipt = False
        operation_id = _pending_reconciliation_operation(result) if owns_write_scope else None
        if operation_id is not None:
            if (receipt_hooks is None or receipt_hooks.operation_id != operation_id
                    or receipt_hooks.verify_receipt is None
                    or receipt_hooks.refresh_response is None):
                logger.error("Workspace receipt callbacks missing for operation %s", operation_id)
                _unknown_reconciliation_receipt(result, operation_id)
            else:
                try:
                    receipt_hooks.verify_receipt()
                    receipt_hooks.refresh_response()
                    verified_receipt = True
                except Exception:
                    logger.exception("Workspace receipt verification failed for operation %s", operation_id)
                    _unknown_reconciliation_receipt(result, operation_id)
        for effect in effects:
            try:
                effect()
            except Exception:
                logger.exception("Workspace post-commit effect failed for turn %s", turn_id)
        # Follow-up suggestions are optional and run after the writer has exited.
        # Their failure must not reclassify a durable project write as failed.
        try:
            follow_up_questions = generate_follow_up_questions(
                user_prompt=body.message,
                answer=result.text,
            )
        except Exception:
            logger.exception("Workspace follow-up generation failed for turn %s", turn_id)
            follow_up_questions = []
        if follow_up_questions:
            result = ExecutionResult(
                text=result.text,
                parts=[
                    *result.parts,
                    {"type": "follow_up_questions", "questions": follow_up_questions},
                ],
            )
        try:
            terminal = _persist_worker_success(
                thread_id=thread_id, turn_id=turn_id, result=result,
            )
        except Exception:
            if not (owns_write_scope or own_draft_write):
                raise
            logger.exception("Workspace reply persistence failed after durable write for turn %s", turn_id)
            # Retry the same result: a transient error must not be saved as a
            # permanent warning in conversation history.
            try:
                terminal = _persist_worker_success(
                    thread_id=thread_id, turn_id=turn_id, result=result,
                )
            except Exception:
                logger.exception("Workspace reply recovery persistence failed for turn %s", turn_id)
                if not verified_receipt:
                    # Without a verified committed receipt, a running turn cannot
                    # be advertised as completed or reclassified as failed.
                    event_queue.put(("unknown", None))
                    return
                _warn_unpersisted_reply(result)
                terminal = {"status": "completed", "result": result, "message_id": None}
        event_queue.put(("completed", terminal))
    except Exception as exc:
        try:
            event_queue.put(("failed", _persist_worker_failure(
                thread_id=thread_id,
                turn_id=turn_id,
                exc=exc,
            )))
        except Exception as persistence_exc:
            terminal = _failure_envelope(exc)
            terminal["persistence_error"] = str(persistence_exc) or persistence_exc.__class__.__name__
            event_queue.put(("failed", terminal))
    finally:
        _unregister_active_turn(turn_id)
        otel_context.detach(token)

def _coarse_progress(step_name: str, status: str, detail: str | None) -> dict[str, str]:
    """Convert internal runtime instrumentation into safe user-visible progress."""
    step = (step_name or "").strip()
    lowered = step.lower()
    tool_match = re.search(r"execute tool:\s*([a-z0-9_]+)", lowered)
    tool_messages = {
        "portfolio_attention": "Analyzing portfolio attention and aging signals",
        "search_project_parties": "Checking project and organisation relationships",
        "query_portfolio": "Querying the active portfolio scope",
        "get_project_brief": "Gathering current project state and recent evidence",
        "web_search": "Searching configured external sources",
    }
    if tool_match:
        message = tool_messages.get(tool_match.group(1), "Checking Workspace records")
        phase = "retrieval"
    elif "context" in lowered:
        message = "Loading Workspace context"
        phase = "context"
    elif "agent round" in lowered or "model" in lowered or "llm" in lowered:
        message = "Analyzing verified records"
        phase = "analysis"
    else:
        safe_detail = (detail or "").strip()
        if re.search(r"monads0x|tool[_ -]?call|chain[- ]of[- ]thought", safe_detail, re.IGNORECASE):
            safe_detail = ""
        message = safe_detail or "Working on the request"
        phase = "working"
    if status == "done" and not message.lower().endswith("complete"):
        message = f"{message} complete"
    elif status == "error":
        message = "A Workspace data step could not be completed"
    return {"phase": phase, "message": message}


def _replay_or_follow_running_turn(turn_id: str, turn: dict[str, Any]) -> Iterator[str]:
    """Replay a terminal turn or follow an in-process worker until it persists one."""
    current = turn
    last_heartbeat = 0.0
    while str(current.get("execution_status") or "") == "running":
        worker = _active_turn(turn_id)
        if worker is None:
            break
        now = time.monotonic()
        if now - last_heartbeat >= _HEARTBEAT_SECONDS:
            yield _sse("progress", {
                "turn_id": turn_id,
                "phase": "running",
                "message": "This turn is still running in the background",
            })
            last_heartbeat = now
        time.sleep(0.25)
        try:
            with scoped_read() as conn:
                refreshed = get_turn(conn, turn_id)
        except Exception:
            logger.exception("Could not poll running turn %s", turn_id)
            yield _unknown_turn_progress(turn_id)
            continue
        if refreshed is None:
            break
        current = refreshed
    if str(current.get("execution_status") or "") == "running":
        # A worker may have exited after committing the project but before it
        # could save the assistant reply. Recover only from its owned receipt;
        # never re-execute a business command on an idempotent client retry.
        try:
            with scoped_read() as conn:
                current = get_turn(conn, turn_id) or current
                recovered = committed_receipt_for_turn(conn, turn=current)
            if recovered is not None:
                try:
                    _persist_worker_success(
                        thread_id=str(current["thread_id"]), turn_id=turn_id, result=recovered,
                    )
                except Exception:
                    logger.exception("Could not persist recovered reconciliation reply for %s", turn_id)
                    _warn_unpersisted_reply(recovered)
                    yield _sse("text.delta", {"turn_id": turn_id, "delta": recovered.text})
                    for part in recovered.parts:
                        if part.get("type") != "text":
                            yield _sse("part.added", {"turn_id": turn_id, "part": part})
                    yield _sse("turn.completed", {"turn_id": turn_id, "message_id": None, "replayed": True})
                    return
                with scoped_read() as conn:
                    current = get_turn(conn, turn_id) or current
        except Exception:
            logger.exception("Could not inspect orphan reconciliation turn %s", turn_id)
        if str(current.get("execution_status") or "") == "running":
            yield _unknown_turn_progress(turn_id)
            return
    yield from _replay_turn(turn_id, current)


def _unknown_turn_progress(turn_id: str) -> str:
    return _sse("progress", {
        "turn_id": turn_id,
        "phase": "unknown",
        "message": (
            "Conversation status is unavailable; the write outcome may be unknown. "
            "Inspect the project before retrying this turn."
        ),
    })


def _replay_turn(turn_id: str, turn: dict[str, Any]) -> Iterator[str]:
    with scoped_read() as conn:
        messages = get_turn_messages(conn, turn_id)
    assistant_messages = [item for item in messages if item.get("role") == "assistant"]
    for message in assistant_messages:
        content = str(message.get("content") or "")
        if content:
            yield _sse("text.delta", {"turn_id": turn_id, "delta": content})
        for part in message.get("parts", []):
            if part.get("type") != "text":
                yield _sse("part.added", {"turn_id": turn_id, "part": part})
    status = turn.get("execution_status")
    if status == "completed":
        yield _sse("turn.completed", {"turn_id": turn_id, "replayed": True})
    elif status == "failed":
        yield _sse("turn.failed", {"turn_id": turn_id, "error": turn.get("error") or "Turn failed"})
    else:
        yield _sse("progress", {
            "turn_id": turn_id,
            "phase": "running",
            "message": "This turn is already running",
        })


def _sse(event: str, data: dict[str, Any]) -> str:
    return f"event: {event}\ndata: {json.dumps(validate_assistant_stream_event(event, data), ensure_ascii=False)}\n\n"
