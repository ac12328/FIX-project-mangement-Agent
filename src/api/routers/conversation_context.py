"""Conversation context endpoints — show what the agent sees before the next reply."""

import json
import math
import sqlite3

from fastapi import APIRouter, Depends, Query
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from config.settings import CONFIG
from src.api.deps import get_conn, get_write_conn
from src.repositories.working_memory_repo import get_memory, upsert_memory

router = APIRouter(tags=["conversation_context"])


# ── Context Preview schemas ───────────────────────────────────────────


class ContextPreviewRequest(BaseModel):
    message: str
    chat_id: int | None = None


class ContextLayerResponse(BaseModel):
    name: str
    label: str
    content: str
    token_estimate: int


class ContextPreviewResponse(BaseModel):
    layers: list[ContextLayerResponse]
    intent: str
    detected_project_id: str | None
    available_tools: list[str]
    missing_flags: list[str]
    context_confidence: float
    total_token_estimate: int
    skills_block: str
    planning_hint: bool
    profile: str = "full"
    host_token_estimate: int = 0
    specialist_eligible: bool = False


def _get_recent_messages(conn: sqlite3.Connection, chat_id: int, limit: int = 10) -> list[dict]:
    """Same query as agent_chat._get_recent_conversation — kept in sync."""
    rows = conn.execute(
        """SELECT direction, text, message_kind, created_at
           FROM telegram_messages
           WHERE chat_id = ? AND text IS NOT NULL AND text != ''
             AND message_kind IN ('text', 'system', 'command', 'command_reply')
           ORDER BY created_at DESC
           LIMIT ?""",
        (chat_id, limit),
    ).fetchall()
    return [dict(row) for row in reversed(rows)]


def _resolve_chat_id(chat_id: int | None) -> int | None:
    if chat_id is not None:
        return chat_id
    return getattr(getattr(CONFIG, "telegram", None), "chat_id", None)


@router.get("/conversation-context")
def get_conversation_context(
    chat_id: int | None = Query(None),
    conn: sqlite3.Connection = Depends(get_conn),
):
    resolved = _resolve_chat_id(chat_id)
    if not resolved:
        return JSONResponse(status_code=400, content={"message": "No chat_id provided or configured"})

    key = f"conversation_summary_{resolved}"
    mem = get_memory(conn, "_system", key)

    # New structured progress
    progress_key = f"agent_progress_{resolved}"
    progress_mem = get_memory(conn, "_system", progress_key)
    progress = None
    if progress_mem and progress_mem["memory_value"]:
        try:
            progress = json.loads(progress_mem["memory_value"])
        except (json.JSONDecodeError, TypeError):
            progress = None

    recent = _get_recent_messages(conn, resolved)

    return {
        "chat_id": resolved,
        "summary": mem["memory_value"] if mem else None,
        "summary_updated_at": mem["updated_at"] if mem else None,
        "progress": progress,
        "recent_messages": recent,
    }


@router.delete("/conversation-context")
def clear_conversation_summary(
    chat_id: int | None = Query(None),
    conn: sqlite3.Connection = Depends(get_write_conn),
):
    resolved = _resolve_chat_id(chat_id)
    if not resolved:
        return JSONResponse(status_code=400, content={"message": "No chat_id provided or configured"})

    key = f"conversation_summary_{resolved}"
    recent = _get_recent_messages(conn, resolved, limit=20)
    if recent:
        from src.learning.outbox import make_turn_key
        from src.repositories.learning_outbox_repo import enqueue_learning

        messages = [
            {
                "role": "user" if row.get("direction") == "inbound" else "assistant",
                "content": row.get("text") or "",
            }
            for row in recent
            if row.get("text")
        ]
        enqueue_learning(
            conn,
            idempotency_key=f"clear:{make_turn_key('telegram', str(resolved), messages)}",
            surface="conversation_clear",
            conversation_id=str(resolved),
            messages=messages,
            round_count=2,
            tool_call_count=0,
            notify_chat_id=resolved,
        )
    upsert_memory(conn, "_system", key, None)
    upsert_memory(conn, "_system", f"agent_progress_{resolved}", None)
    conn.commit()
    return {"ok": True}


# ── Context Preview endpoint ──────────────────────────────────────────


def _estimate_tokens(text: str) -> int:
    if not text:
        return 0
    return math.ceil(len(text) / 4)


@router.post("/context-preview")
def preview_context(
    body: ContextPreviewRequest,
    conn: sqlite3.Connection = Depends(get_conn),
):
    from config import defaults
    from src.agent.intent import resolve_tool_intent
    from src.context.engine import ContextEngine
    from src.context.skills_index import build_skills_block
    from src.telegram.agent_chat import (
        _classify_intent,
        _detect_project_id,
        _infer_project_from_conversation,
        _is_casual_message,
        _is_complex_request,
        _is_portfolio_query,
    )
    from src.tools.registry import get_tools_for_intent

    resolved = _resolve_chat_id(body.chat_id)
    if not resolved:
        return JSONResponse(status_code=400, content={"message": "No chat_id provided or configured"})

    message = body.message.strip()
    if not message:
        return JSONResponse(status_code=400, content={"message": "Message cannot be empty"})

    base_intent = _classify_intent(message)
    orchestrator_on = getattr(defaults, "ORCHESTRATOR_MODE", False)
    tool_intent = resolve_tool_intent(base_intent, orchestrator_mode=orchestrator_on)

    casual = _is_casual_message(message)
    portfolio = _is_portfolio_query(message)
    is_email = message.startswith("[INBOUND EMAIL]") or message.startswith("[INBOUND REPLY]")
    orchestrator_host = orchestrator_on and base_intent not in ("email", "casual") and not is_email
    profile = (
        "bootstrap" if orchestrator_host
        else "minimal" if tool_intent == "query" and not _detect_project_id(message)
        else "full"
    )

    explicit_pid = _detect_project_id(message)
    detected_project_id = explicit_pid or (
        None if (casual or portfolio) else _infer_project_from_conversation(conn, resolved)
    )

    from src.agent.memory_loop_helpers import context_operator_id

    engine = ContextEngine(conn)
    frame = engine.build_for_web_chat(
        user_text=message,
        project_id=detected_project_id,
        profile=profile,
        operator_id=context_operator_id(conn),
        session_id=f"context-preview:{resolved}",
    )

    tools = get_tools_for_intent(tool_intent)
    tool_names = [t["function"]["name"] for t in tools]

    skills_block = "" if profile == "bootstrap" else build_skills_block()
    planning_hint = _is_complex_request(message)

    layer_defs = [
        ("identity", "System Prompt", frame.identity),
        ("knowledge", "Knowledge", frame.knowledge),
        ("project_truth", "Project Truth", frame.project_truth),
        ("evidence", "Evidence", frame.evidence),
        ("relationship", "Relationship", frame.relationship),
        ("memory", "Memory", frame.memory),
        ("conversation", "Conversation History", json.dumps(frame.conversation, ensure_ascii=False, indent=2) if frame.conversation else ""),
    ]
    if profile != "bootstrap":
        layer_defs.append(("skills", "Skills Index", skills_block))
    layer_defs.append(("instructions", "Instructions", frame.instructions))

    layers = []
    total_tokens = 0
    for name, label, content in layer_defs:
        tokens = _estimate_tokens(content)
        total_tokens += tokens
        layers.append(ContextLayerResponse(
            name=name,
            label=label,
            content=content,
            token_estimate=tokens,
        ))

    total_tokens += _estimate_tokens(message)

    specialist_eligible = orchestrator_host and tool_intent == "orchestrator"
    host_token_estimate = total_tokens + _estimate_tokens(message)

    return ContextPreviewResponse(
        layers=layers,
        intent=tool_intent,
        detected_project_id=detected_project_id,
        available_tools=tool_names,
        missing_flags=frame.missing_flags,
        context_confidence=frame.context_confidence,
        total_token_estimate=total_tokens,
        skills_block=skills_block,
        planning_hint=planning_hint,
        profile=profile,
        host_token_estimate=host_token_estimate,
        specialist_eligible=specialist_eligible,
    )
