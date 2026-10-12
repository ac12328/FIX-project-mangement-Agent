"""Pre-compression memory flush: saves learnings to wiki/agent_memory.

Extracted from agent_chat.py for the 500-line convention.
"""

from __future__ import annotations

import logging
import sqlite3
from typing import Any

from src.llm.client import llm_call_with_tools
from src.agent.safety import normalize_assistant_message
from src.tools.executor import execute_tool_calls

logger = logging.getLogger(__name__)


def _flush_memories(
    conn: sqlite3.Connection | None,
    chat_id: int,
    messages: list[dict],
    round_count: int,
    tool_call_count: int,
    *,
    host_surface: bool = True,
    project_id: str | None = None,
) -> None:
    """Enqueue durable post-turn learning for non-trivial host interactions."""
    if not host_surface or round_count < 2:
        return
    try:
        from src.learning.outbox import capture_learning
        from src.agent.memory_loop_helpers import resolve_loop_operator_id

        capture_learning(
            conn,
            surface="telegram",
            conversation_id=str(chat_id),
            messages=messages,
            round_count=round_count,
            tool_call_count=tool_call_count,
            notify_chat_id=chat_id,
            operator_id=resolve_loop_operator_id(conn, chat_id=chat_id),
            project_id=project_id,
        )
    except Exception:
        logger.debug("Failed to enqueue memory flush", exc_info=True)


def _build_existing_state_summary() -> str:
    """Build a summary of existing agent notes and wiki pages for dedup."""
    from src.db.connection import scoped_read

    parts: list[str] = []

    try:
        from src.repositories.agent_notes_repo import list_notes, total_chars, CHAR_LIMIT
        with scoped_read() as conn:
            notes = list_notes(conn)
            if notes:
                total = total_chars(conn)
                lines = [f"- {n['note_key']}: {n['note_value']}" for n in notes]
                parts.append(
                    f"## Existing agent notes ({total}/{CHAR_LIMIT} chars)\n"
                    + "\n".join(lines)
                )
    except Exception:
        logger.debug("Failed to load existing notes for flush", exc_info=True)

    try:
        from pathlib import Path
        wiki_dir = Path("wiki")
        if wiki_dir.is_dir():
            pages = sorted(
                str(p.relative_to(wiki_dir)).replace("\\", "/").removesuffix(".md")
                for p in wiki_dir.rglob("*.md")
                if p.name not in ("index.md", "log.md", "schema.md", "lint_report.md")
                and "raw" not in p.parts
            )
            if pages:
                lines = [f"- {p}" for p in pages]
                parts.append(
                    "## Existing wiki pages\n" + "\n".join(lines)
                )
    except Exception:
        logger.debug("Failed to list wiki pages for flush", exc_info=True)

    if not parts:
        return ""
    return (
        "\n".join(parts)
        + "\n\nDo NOT duplicate existing entries. "
        "Use `agent_memory` action=`replace` to update an existing note if the new info refines it. "
        "Use `wiki_update` instead of `wiki_create` if a relevant page already exists.\n"
    )


def _do_flush_impl(
    chat_id: int,
    messages: list[dict],
    round_count: int,
    tool_call_count: int,
) -> dict[str, list[str]]:
    """Execute the learning flush and propagate processing errors."""

    flush_prompt = (
        "This conversation is ending. Review what happened and save anything "
        "worth remembering for future sessions.\n\n"
        "## Process (MUST follow this order)\n"
        "1. THINK: Call `think` to reason about what new knowledge appeared\n"
        "2. CHECK: For each candidate, call `wiki_search` to verify it's not already captured\n"
        "3. SAVE: Only create/update if genuinely new. One fact per entry.\n"
        "4. LINK: For every page you create or update, add at least 2 typed links to "
        "related existing pages. Use `wiki_link` or include `links:` in frontmatter.\n"
        "5. STOP: If nothing new, do nothing — do NOT create low-value entries.\n\n"
        "## Linking discipline (CRITICAL — this is what makes the wiki compound)\n\n"
        "The wiki's value comes from CONNECTIONS, not isolated pages. Every page MUST "
        "be connected to the graph. When you create or update a page:\n"
        "- Search for related entities, concepts, markets, and patterns\n"
        "- Add typed links in frontmatter: depends_on, relates_to, operates_in, "
        "uses, used_by, requires, supersedes, contradicts, derived_from\n"
        "- If a new fact touches an existing page, UPDATE that page too (add a link back)\n"
        "- A single piece of knowledge should touch 2-5 wiki pages, not just 1\n"
        "- Entity pages should link to markets they operate in\n"
        "- Concepts should link to patterns that implement them\n"
        "- Patterns should link to concepts they derive from\n\n"
        "Example: Learning 'grey market orders rejected before market open' should:\n"
        "  1. wiki_update markets/hk_market.md (add the rule)\n"
        "  2. wiki_link from markets/hk_market to concept/conformance_testing (relates_to)\n"
        "  3. wiki_link from any relevant entity page to markets/hk_market (operates_in)\n\n"
        "## What to capture\n\n"
        "### Our company's domain knowledge → wiki\n"
        "REUSABLE rules/procedures that apply across ALL projects and remain true over time:\n"
        "- Our trading rules & policies (e.g. 'grey market orders before market open are rejected')\n"
        "- Our FIX protocol requirements (tag usage, session config, order handling)\n"
        "- Our onboarding procedures and requirements for counterparties\n"
        "- Market-specific rules (order types, cut-off times, settlement)\n"
        "- Technical setup rules (VPN config, network requirements, gateway behavior)\n"
        "- Standard responses to common client questions\n"
        "→ Save via `wiki_create`/`wiki_update`. ALWAYS include `links:` in frontmatter.\n\n"
        "### Operator preferences → agent_memory\n"
        "- Corrections ('don't do X', 'always do Y')\n"
        "- Communication style preferences\n"
        "- Workflow preferences\n"
        "→ Save via `agent_memory`.\n\n"
        "## NEVER save (critical — read every line)\n"
        "- Project-specific events belong in scoped project notes, not general memory.\n"
        "- Current status snapshots are not reusable general memory.\n"
        "- One-time actions are not reusable general memory.\n"
        "- Person-specific confirmations remain scoped evidence.\n"
        "- Client-specific endpoints and failures remain scoped evidence.\n"
        "- Tool availability, role scope, delegation rules, confirmation semantics, or system contracts ← NO\n"
        "- Workarounds inferred from a failed/exhausted agent turn ← NO\n"
        "These belong in versioned code/contracts or the project's activity_log/notes, NOT wiki or agent_memory.\n\n"
        "## Reasoning test\n"
        "Before saving ANYTHING, ask yourself:\n"
        "  'Would this still be true and useful in 6 months, for a DIFFERENT project?'\n"
        "  'Does it mention a specific project ID, date, person, or IP address?'\n"
        "If the answer to the first question is NO, or the second is YES → do NOT save.\n\n"
        "Examples:\n"
        "  ✓ 'PCT requires client to connect via Primary IP before sending test order' → wiki (general rule)\n"
        "  ✓ 'Operator prefers Traditional Chinese responses' → agent_memory (preference)\n"
        "- Do not generalize project-specific dates, people, or confirmations.\n"
        "- Do not generalize project-specific dates, people, or confirmations.\n"
        "Keep each entry short and specific. If nothing new was learned, do nothing.\n"
    )
    if tool_call_count >= 8:
        flush_prompt += (
            "\nThis was a complex multi-step workflow. Consider saving it as a "
            "reusable procedure via `wiki_create` with type 'playbook' and tags "
            "[agent-authored, auto-skill]. Include: when to use it, the steps taken, "
            "and any edge cases discovered. First use `wiki_search` to check if a "
            "similar procedure already exists — if so, use `wiki_update` to patch it. "
            "Link the playbook to all concepts/entities/markets it touches."
        )
    if round_count >= 3:
        flush_prompt += (
            "\n\n## Filing answers back into the wiki\n"
            "If this conversation produced a synthesis, comparison, or explanation that "
            "would help answer similar questions in the future, file it into the wiki. "
            "Good candidates: explanations of how two systems interact, comparisons "
            "between approaches, troubleshooting guides that combine multiple pieces of knowledge. "
            "Always link the new page to its component knowledge pages."
        )

    from src.tools.registry import (
        AGENT_MEMORY, WIKI_SEARCH, WIKI_READ, WIKI_CREATE, WIKI_UPDATE,
        WIKI_LINK, _to_openai_schema,
    )
    flush_tool_schemas = _to_openai_schema([
        AGENT_MEMORY, WIKI_SEARCH, WIKI_READ, WIKI_CREATE, WIKI_UPDATE,
        WIKI_LINK,
    ])

    existing_state = _build_existing_state_summary()
    if existing_state:
        flush_prompt += "\n" + existing_state

    flush_messages = messages + [{"role": "system", "content": flush_prompt}]

    saved_items: list[str] = []
    max_flush_rounds = 5
    for _ in range(max_flush_rounds):
        response = llm_call_with_tools(
            "flush", flush_messages, flush_tool_schemas, temperature=0.3,
        )
        flush_messages.append(normalize_assistant_message(response))

        tool_calls_result = response.get("tool_calls")
        if not tool_calls_result:
            break

        tool_results = execute_tool_calls(None, "flush", tool_calls_result)
        flush_messages.extend(tool_results)

        import json as _json
        for tc, tool_message in zip(tool_calls_result, tool_results):
            fn = tc.get("function", {})
            name = fn.get("name", "")
            try:
                args = _json.loads(fn.get("arguments", "{}"))
            except Exception:
                args = {}
            try:
                outcome = _json.loads(tool_message.get("content", "{}"))
            except Exception:
                outcome = {"error": "invalid tool result"}
            if not isinstance(outcome, dict) or outcome.get("error"):
                continue
            if name == "wiki_create" and outcome.get("success") and outcome.get("verified"):
                path, revision = outcome.get("record_id"), outcome.get("version")
                if path and revision:
                    saved_items.append(f"  Wiki {path} (revision: {revision})")
            elif name == "wiki_update" and outcome.get("success") and outcome.get("verified"):
                path, revision = outcome.get("record_id"), outcome.get("version")
                if path and revision:
                    saved_items.append(f"  Wiki updated {path} (revision: {revision})")
            elif name == "agent_memory" and outcome.get("success") and outcome.get("verified"):
                action = args.get("action", "add")
                verb = "Note removed" if action == "remove" else "Note"
                saved_items.append(
                    f"  {verb} {outcome['record_id']} (version: {outcome['version']})"
                )
                if action != "remove":
                    _dual_write_flush_memory(chat_id, args, action=action)

    if saved_items:
        _notify_learning(chat_id, "\n".join(saved_items))
    return {"saved_items": saved_items}


def _dual_write_flush_memory(chat_id: int, args: dict, *, action: str) -> None:
    """Mirror agent_memory flush writes into operator_memory when enabled."""
    from src.memory.reform_flags import memory_flush_to_store
    if not memory_flush_to_store():
        return
    from src.learning.legacy_retirement import get_retirement_state, RetirementMode
    from src.db.connection import scoped_read
    with scoped_read() as retirement_conn:
        if get_retirement_state(retirement_conn).mode is RetirementMode.RETIRED:
            return
    key = args.get("key", "")
    value = args.get("value", "")
    if not key or not value:
        return
    try:
        from src.db.connection import scoped_write
        from src.agent.operator_identity import resolve_operator_id
        from src.memory.operator_memory_store import OperatorMemoryStore
        from src.models.operator_memory import MemoryType

        note_type = args.get("note_type", "knowledge")
        memory_type = MemoryType.PROCEDURAL if note_type == "pattern" else MemoryType.SEMANTIC
        with scoped_write() as conn:
            store = OperatorMemoryStore(conn, resolve_operator_id(conn, chat_id=chat_id))
            store.put(
                scope="global",
                scope_id="*",
                memory_type=memory_type,
                memory_key=key,
                memory_value={"description": value, "text": value, "flush_action": action},
                source="memory_flush",
            )
            conn.commit()
    except Exception:
        logger.debug("Dual-write flush to operator_memory failed", exc_info=True)


def _do_flush(
    chat_id: int,
    messages: list[dict],
    round_count: int,
    tool_call_count: int,
) -> dict[str, list[str]]:
    """Compatibility wrapper: direct flush calls remain best-effort."""
    try:
        return _do_flush_impl(chat_id, messages, round_count, tool_call_count)
    except Exception:
        logger.debug("Pre-compression flush failed", exc_info=True)
        return {"saved_items": []}


def run_flush_job(
    chat_id: int,
    messages: list[dict],
    round_count: int,
    tool_call_count: int,
) -> dict[str, list[str]]:
    """Run one durable learning job and propagate failures for retry."""
    from src.learning.governance_flags import candidate_only
    if candidate_only():
        return {"capture_only": True, "saved_items": []}
    return _do_flush_impl(chat_id, messages, round_count, tool_call_count)


def run_flush_sync_for_eval(
    chat_id: int,
    messages: list[dict],
    round_count: int,
    tool_call_count: int,
) -> None:
    """Synchronous flush for eval harness (EVAL_SYNC_FLUSH=1)."""
    import os
    if os.environ.get("EVAL_SYNC_FLUSH") != "1":
        return
    run_flush_job(chat_id, messages, round_count, tool_call_count)


def _notify_learning(chat_id: int, summary: str) -> None:
    """Send a brief Telegram message about what the agent learned. Best-effort."""
    try:
        import requests
        from config.settings import CONFIG

        token = CONFIG.telegram.bot_token
        if not token:
            return
        # Prefer HTML (reliable over raw HTTP without Rich Message client helpers).
        import html as _html

        body = _html.escape(summary[:3800])
        text = f"<i>Learned and saved:</i>\n{body}"
        requests.post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            json={
                "chat_id": chat_id,
                "text": text,
                "parse_mode": "HTML",
                "disable_web_page_preview": True,
            },
            timeout=10,
        )
    except Exception:
        logger.debug("Learning notification send failed", exc_info=True)
