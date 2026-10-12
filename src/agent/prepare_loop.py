"""Unified agent loop policy — intent, tools, guards, round budget."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import dataclass, field
from typing import Any

from src.agent.guards import (
    blocked_tools_for_message,
    build_guard_system_messages,
    filter_tools_by_allowlist,
    is_bare_update_command,
    is_staged_update_confirm_intent,
    knowledge_query_tool_allowlist,
    portfolio_followup_tool_allowlist,
)
from config import defaults
from src.agent.intent import classify_intent, label_intent, resolve_tool_intent
from src.agent.loop_config import InferenceSurface
from src.agent.round_budget import resolve_max_rounds
from src.tools.registry import ALL_TOOLS, _to_openai_schema, get_tools_for_intent
from src.tools.role_tools import AgentRole, get_tools_for_role

_PROGRESS_GUARD_MARKERS = (
    "progress-advisory-guard",
    "m8-signoff-ready-guard",
    "portfolio-progress-advisory-guard",
)

_PROGRESS_EXTRA_TOOLS = frozenset({"get_advisory"})


def _merge_tools_by_name(
    tools: list[dict],
    extra_names: frozenset[str],
) -> list[dict]:
    have = {t.get("function", {}).get("name") for t in tools}
    missing = extra_names - have
    if not missing:
        return tools
    contracts = [
        t for t in ALL_TOOLS
        if t.llm_visible and t.name in missing
    ]
    return tools + _to_openai_schema(contracts)


def _progress_guard_active(extra_messages: list[dict]) -> bool:
    for msg in extra_messages:
        content = msg.get("content") or ""
        if any(marker in content for marker in _PROGRESS_GUARD_MARKERS):
            return True
    return False


def _tool_memory_message(
    conn: sqlite3.Connection | None,
    project_id: str | None,
    tools: list[dict[str, Any]],
    intent: str,
    operator_id: str | None,
) -> tuple[dict[str, str], Any] | None:
    """Build governed Tool-surface context for the actual host model prompt."""
    if conn is None or not project_id or not tools:
        return None
    tool_names = sorted(
        name for tool in tools
        if (name := tool.get("function", {}).get("name"))
    )
    task_context = f"host:{intent}:{','.join(tool_names)}"
    try:
        from src.context.engine import ContextEngine

        frame = ContextEngine(conn).build_for_tool(
            project_id, task_context, operator_id=operator_id,
        )
    except sqlite3.Error:
        return None
    if frame is None or not frame.memory:
        return None
    scope = None
    if operator_id:
        scope = frame.memory_exposure_scope(
            operator_id=operator_id, surface="tool",
            task_type=task_context, project_id=project_id,
        )
    return ({
        "role": "system",
        "content": f"<tool-context>\n{frame.memory}\n</tool-context>",
    }, scope)


def _skill_routing_message(user_text: str) -> dict[str, str] | None:
    """Inject matched skill_ids so the host prefers the right delegate call."""
    if not user_text or not getattr(defaults, "ORCHESTRATOR_MODE", False):
        return None
    from src.agent.skills.registry import match_skills

    hits = match_skills(user_text)
    if not hits:
        return None
    # Cap to top 3 to avoid drowning the host
    top = hits[:3]
    lines = [
        "<skill-routing>",
        "Host is read+delegate only. For writes, call delegate with skill_id:",
    ]
    for s in top:
        lines.append(f"- Prefer skill_id=`{s.skill_id}` — {s.title}: {s.description[:120]}")
    lines.append("</skill-routing>")
    return {"role": "system", "content": "\n".join(lines)}


@dataclass
class PreparedLoop:
    intent: str
    tools: list[dict[str, Any]]
    surface: InferenceSurface = "chat"
    extra_messages: list[dict[str, Any]] = field(default_factory=list)
    exposure_scopes: tuple[Any, ...] = ()
    blocked_tool_names: frozenset[str] = field(default_factory=frozenset)
    max_rounds: int = 8
    preparation_version: str = "prepare-loop-v1"
    snapshot: dict[str, Any] = field(default_factory=dict)


def _tools_for_unified_v2(intent_label: str) -> list[dict[str, Any]]:
    """Role-based tool selection for unified loop v2 (intent is label-only)."""
    if intent_label == "casual":
        return []
    if getattr(defaults, "ORCHESTRATOR_MODE", False):
        return get_tools_for_role(AgentRole.HOST)
    return get_tools_for_role(AgentRole.GENERAL)


def prepare_loop(
    conn: sqlite3.Connection | None,
    user_text: str,
    *,
    effective_project_id: str | None = None,
    chat_id: int | None = None,
    max_rounds_override: int | None = None,
    surface: InferenceSurface = "chat",
    unified_v2: bool = False,
    operator_id: str | None = None,
) -> PreparedLoop:
    """Resolve intent, tool list, guard injections, and round budget for one invocation."""
    if unified_v2:
        intent = label_intent(user_text)
        tools = _tools_for_unified_v2(intent)
    else:
        base_intent = classify_intent(user_text)
        intent = resolve_tool_intent(
            base_intent,
            orchestrator_mode=getattr(defaults, "ORCHESTRATOR_MODE", False),
        )
        tools = get_tools_for_intent(intent)
    if is_bare_update_command(user_text):
        tools = []
    elif is_staged_update_confirm_intent(user_text):
        # Host no longer has confirm_update — deterministic shortcut handles it;
        # keep think only so the model cannot invent a write.
        tools = filter_tools_by_allowlist(tools, frozenset({"think"}))

    allowlist = portfolio_followup_tool_allowlist(user_text)
    if allowlist is None:
        allowlist = knowledge_query_tool_allowlist(user_text)
    skip_allowlist_tools = unified_v2 and getattr(defaults, "ORCHESTRATOR_MODE", False)
    if allowlist is not None and not skip_allowlist_tools:
        source_tools = get_tools_for_intent("query") if intent == "orchestrator" else tools
        tools = filter_tools_by_allowlist(source_tools, allowlist)

    blocked = blocked_tools_for_message(
        user_text,
        conn,
        chat_id=chat_id,
        effective_project_id=effective_project_id,
    )
    if blocked:
        tools = [
            t for t in tools
            if t.get("function", {}).get("name") not in blocked
        ]

    extra = build_guard_system_messages(
        user_text,
        conn,
        chat_id=chat_id,
        effective_project_id=effective_project_id,
    )
    skill_hint = _skill_routing_message(user_text)
    if skill_hint:
        extra = [skill_hint, *extra]
    tool_memory = _tool_memory_message(
        conn, effective_project_id, tools, intent, operator_id,
    )
    exposure_scopes = ()
    if tool_memory:
        tool_message, tool_scope = tool_memory
        extra = [tool_message, *extra]
        if tool_scope is not None:
            exposure_scopes = (tool_scope,)
    if _progress_guard_active(extra):
        tools = _merge_tools_by_name(tools, _PROGRESS_EXTRA_TOOLS)
    max_rounds = max_rounds_override if max_rounds_override is not None else resolve_max_rounds(
        intent, user_text
    )

    tool_names = [tool.get("function", {}).get("name", "") for tool in tools]
    snapshot = {
        "intent": intent,
        "surface": surface,
        "tool_names": tool_names,
        "blocked_tool_names": sorted(blocked),
        "max_rounds": max_rounds,
        "extra_message_count": len(extra),
        "tool_hash": hashlib.sha256(
            json.dumps(tools, sort_keys=True, ensure_ascii=False, default=str).encode("utf-8")
        ).hexdigest(),
        "prompt_hash": hashlib.sha256(
            json.dumps(extra, sort_keys=True, ensure_ascii=False, default=str).encode("utf-8")
        ).hexdigest(),
        "policy_version": "unified-loop-policy-v1",
    }
    return PreparedLoop(
        intent=intent,
        surface=surface,
        tools=tools,
        extra_messages=extra,
        exposure_scopes=exposure_scopes,
        blocked_tool_names=blocked,
        max_rounds=max_rounds,
        snapshot=snapshot,
    )