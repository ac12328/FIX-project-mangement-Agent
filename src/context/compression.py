"""
LLM-based context compression (WS-2).

Three-zone compression:
  Head (protected): system prompt at the start — always preserved.
  Middle (compressed): summarized by LLM into <context-summary> fence.
  Tail (protected): last N messages — always preserved verbatim.

If the LLM summarizer fails, falls back to simple truncation
(matching the existing behaviour in context_compression.py).
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any, Callable

# Fields that MUST survive LLM compression verbatim
_CRITICAL_FIELDS_RE = re.compile(
    r'"(?:project_id|current_milestone|ball_with|project_status|draft_id|ticket_id|session_id)":\s*"([^"]+)"'
)
_ANCHOR_ATTR_RE = re.compile(
    r'<loop-anchors[^>]*\s(?:project_id|milestone|ball_with|status)="([^"]+)"'
)

logger = logging.getLogger(__name__)

from config.defaults import COMPRESS_MESSAGE_THRESHOLD, COMPRESS_TAIL_KEEP

# Default: compress when non-system message count exceeds this
_DEFAULT_THRESHOLD = COMPRESS_MESSAGE_THRESHOLD

# Default: keep this many tail messages verbatim
_DEFAULT_TAIL_KEEP = COMPRESS_TAIL_KEEP


def should_compress(
    messages: list[dict[str, Any]],
    *,
    threshold: int = _DEFAULT_THRESHOLD,
) -> bool:
    """Return True if the conversation is long enough to warrant compression."""
    count = sum(1 for m in messages if m.get("role") != "system")
    return count > threshold


def compress_with_llm(
    messages: list[dict[str, Any]],
    *,
    summarize_fn: Callable[[str], str] | None = None,
    threshold: int = _DEFAULT_THRESHOLD,
    tail_keep: int = _DEFAULT_TAIL_KEEP,
) -> list[dict[str, Any]]:
    """Compress a conversation using head/tail protection + LLM middle summary.

    Args:
        messages: Full conversation message list (including system prompt).
        summarize_fn: ``fn(text) -> summary_str`` that calls an LLM.
            If None, falls back to truncation.
        threshold: Non-system message count threshold before compression triggers.
        tail_keep: Number of tail messages to preserve verbatim.

    Returns:
        Compressed message list: [head] + [summary_msg] + [tail].
    """
    non_system = [m for m in messages if m.get("role") != "system"]
    if len(non_system) <= threshold:
        return messages

    # Extract head (system messages at the start)
    head: list[dict[str, Any]] = []
    body_start = 0
    for i, msg in enumerate(messages):
        if msg.get("role") == "system":
            head.append(msg)
            body_start = i + 1
        else:
            break

    body = messages[body_start:]

    if len(body) <= tail_keep:
        return messages

    middle = body[: len(body) - tail_keep]
    tail = body[len(body) - tail_keep:]

    if summarize_fn is None:
        logger.debug(
            "No summarizer provided; truncating %d middle messages", len(middle)
        )
        return head + tail

    text = _format_for_summary(middle)

    try:
        summary = summarize_fn(text)
    except Exception:
        logger.warning("LLM compression failed; falling back to truncation", exc_info=True)
        return head + tail

    if not summary:
        return head + tail

    summary_msg: dict[str, Any] = {
        "role": "user",
        "content": (
            "<context-summary>\n"
            f"{summary}\n"
            "</context-summary>"
        ),
    }

    return head + [summary_msg] + tail


def _format_for_summary(messages: list[dict[str, Any]]) -> str:
    """Format messages into readable text for the LLM summarizer."""
    lines: list[str] = []
    critical_facts: set[str] = set()
    for msg in messages:
        role = msg.get("role", "unknown")
        content = msg.get("content", "")
        if not content:
            continue
        # Extract critical fields before any truncation so they survive the summary
        for m in _CRITICAL_FIELDS_RE.finditer(content):
            critical_facts.add(m.group(0))
        for m in _ANCHOR_ATTR_RE.finditer(content):
            critical_facts.add(m.group(0))
        if role == "tool":
            name = msg.get("name", "tool")
            lines.append(f"[Tool: {name}] {content[:300]}")
        else:
            # Preserve existing context-summary blocks for iterative re-summarization
            lines.append(f"{role.title()}: {content[:500]}")

    result: list[str] = []
    if critical_facts:
        result.append(
            "CRITICAL FACTS (preserve these values verbatim in your summary): "
            + "; ".join(sorted(critical_facts))
        )
    result.extend(lines)
    return "\n".join(result)

def _assistant_tool_call_ids(message: dict[str, Any]) -> set[str]:
    return {
        str(call.get("id") or "")
        for call in message.get("tool_calls") or []
        if call.get("id")
    }


def _paired_tail_start(messages: list[dict[str, Any]], tail_start: int) -> int:
    """Move a tail boundary backward so tool results keep their assistant call."""
    if tail_start <= 0 or tail_start >= len(messages):
        return max(0, tail_start)
    first = messages[tail_start]
    if first.get("role") != "tool":
        return tail_start

    tool_call_id = str(first.get("tool_call_id") or "")
    for index in range(tail_start - 1, -1, -1):
        message = messages[index]
        if message.get("role") == "assistant":
            call_ids = _assistant_tool_call_ids(message)
            if tool_call_id and tool_call_id in call_ids:
                return index
            if not tool_call_id and call_ids:
                return index
        if message.get("role") == "user":
            break
    return tail_start


def compress_deterministically(
    messages: list[dict[str, Any]],
    *,
    threshold: int = _DEFAULT_THRESHOLD,
    tail_keep: int = _DEFAULT_TAIL_KEEP,
    protected_user_contents: tuple[str, ...] = (),
) -> list[dict[str, Any]]:
    """Compress without an LLM while preserving exact request and tool pairing.

    Older assistant prose and raw tool payloads are omitted rather than summarized,
    so compression cannot invent facts. The harness injects a typed
    ``<loop-state-capsule>`` containing bounded execution metadata immediately
    after this step.
    """
    if not should_compress(messages, threshold=threshold):
        return messages

    # Prior compression markers are replaceable runtime metadata. Remove them
    # before selecting the tail so a marker at the boundary cannot hide a tool
    # result whose matching assistant call must also be retained.
    working_messages = [
        message
        for message in messages
        if "<deterministic-context-compression"
        not in str(message.get("content") or "")
    ]

    tail_keep = max(1, int(tail_keep))
    tail_start = _paired_tail_start(
        working_messages,
        max(0, len(working_messages) - tail_keep),
    )
    protected_contents = {
        content for content in protected_user_contents if isinstance(content, str) and content
    }
    keep_indexes = set(range(tail_start, len(working_messages)))
    for index, message in enumerate(working_messages[:tail_start]):
        content = message.get("content")
        if message.get("role") == "system" or content in protected_contents:
            keep_indexes.add(index)

    omitted_count = len(messages) - len(keep_indexes)
    if omitted_count <= 0:
        return messages

    prefix = [
        message
        for index, message in enumerate(working_messages[:tail_start])
        if index in keep_indexes
    ]
    summary_message = {
        "role": "user",
        "content": (
            f'<deterministic-context-compression omitted_messages="{omitted_count}">\n'
            "Older messages were omitted deterministically without model "
            "summarization. Do not infer omitted facts. The exact operator request "
            "is preserved, and current execution metadata is provided in the "
            "loop-state capsule.\n"
            "</deterministic-context-compression>"
        ),
    }
    return prefix + [summary_message] + working_messages[tail_start:]


# Surface conversation compaction compatibility API
_IDENTITY_BLOCK = (
    "<identity>\n"
    "You are AgentFIXBPro, a FIX protocol onboarding PM copilot.\n"
    "You're in a Telegram chat with your operator.\n"
    "You have tools to query, update projects, manage contacts, and draft emails.\n"
    "</identity>"
)

# Tool results older than this many turns get micro-compacted
_RECENT_TOOL_TURNS = 3

from config.defaults import COMPRESS_SUMMARY_THRESHOLD

# When conversation exceeds this, oldest messages are summarized
_SUMMARY_THRESHOLD = COMPRESS_SUMMARY_THRESHOLD

_PRESERVE_ID_KEYS = (
    "project_id", "operation_id", "draft_id", "ticket_id", "session_id",
    "artifact_id", "action_id", "contact_id", "email_id", "evidence_id",
    "followup_id", "record_id", "thread_id", "work_item_id",
)
_PRESERVE_STATE_KEYS = (
    "project_status", "current_milestone", "ball_with", "next_action",
    "status", "outcome", "completion_status", "committed",
    "error_code", "side_effect_status",
)
_MAX_COMPACT_FIELD_CHARS = 240


def _compact_scalar(value, *, limit: int = _MAX_COMPACT_FIELD_CHARS):
    if isinstance(value, (bool, int, float)):
        return value
    text = str(value)
    return text if len(text) <= limit else text[: max(0, limit - 3)] + "..."


def _normalise_ids(value: object) -> dict[str, str]:
    if not isinstance(value, dict):
        return {}
    return {
        str(key)[:80]: str(item)[:_MAX_COMPACT_FIELD_CHARS]
        for key, item in value.items()
        if item not in (None, "")
    }


def _normalise_state(value: object) -> dict[str, object]:
    if not isinstance(value, dict):
        return {}
    return {
        key: _compact_scalar(value[key])
        for key in _PRESERVE_STATE_KEYS
        if value.get(key) not in (None, "")
    }


def _compact_text_from_tool_dict(parsed: dict, tool_name: str) -> str:
    """Build human-usable compact text from heterogeneous tool payloads."""
    # Standard text tools
    for key in ("text", "error", "summary", "message"):
        val = parsed.get(key)
        if val:
            return str(val)[:200]

    # get_project_truth shape: {project: {project_id, current_milestone, ...}}
    project = parsed.get("project")
    if isinstance(project, dict) and project:
        pid = project.get("project_id") or ""
        bits = [
            f"{k}={project.get(k)}"
            for k in (
                "project_status",
                "current_milestone",
                "ball_with",
                "next_action",
            )
            if project.get(k) not in (None, "")
        ]
        head = f"{pid}: " if pid else ""
        return (head + ", ".join(bits))[:200] if bits else (pid or "")[:200]

    # ToolDigest after digest_tool_result (summary-only tools)
    if tool_name and parsed.get("tool_name") and parsed.get("summary"):
        return str(parsed["summary"])[:200]

    return ""


def compact_tool_result(msg: dict) -> str:
    """Structured digest preserving critical IDs from tool JSON results."""
    content = msg.get("content", "") or ""
    tool_name = msg.get("name", "tool")
    try:
        parsed = json.loads(content)
    except (json.JSONDecodeError, TypeError):
        parsed = None

    if isinstance(parsed, dict):
        # Re-compacting a prior digest must not erase IDs or state.
        ids = _normalise_ids(parsed.get("ids"))
        ids.update(_normalise_ids({
            key: parsed[key]
            for key in _PRESERVE_ID_KEYS
            if parsed.get(key) not in (None, "")
        }))
        state = _normalise_state(parsed.get("state"))
        state.update(_normalise_state(parsed))
        # Nested project truth payload.
        proj = parsed.get("project")
        if isinstance(proj, dict):
            if proj.get("project_id") and "project_id" not in ids:
                ids["project_id"] = str(proj["project_id"])[:_MAX_COMPACT_FIELD_CHARS]
            state.update(_normalise_state(proj))
        text = str(parsed.get("text") or _compact_text_from_tool_dict(parsed, tool_name))[:200]
        ok = parsed.get("ok", True)
        if parsed.get("error"):
            ok = False
        digest_tool = str(parsed.get("tool") or tool_name)
        digest = {
            "tool": digest_tool,
            "ok": ok,
            "ids": ids,
            "state": state,
            "text": text,
        }
        return json.dumps(digest, ensure_ascii=False, sort_keys=True)

    pid_match = re.search(r"\b[A-Z][A-Z0-9_]+-\d{2,}\b", content)
    pid_suffix = f" on {pid_match.group()}" if pid_match else ""
    return f"[Used {tool_name}{pid_suffix}]"


def micro_compact_tool_results(messages: list[dict], recent_turns: int = _RECENT_TOOL_TURNS) -> list[dict]:
    """Replace old tool-result content with short summaries (Layer 1).

    Messages within the last ``recent_turns`` user/assistant exchanges are
    kept intact.  Older tool-role messages get their content replaced with
    a compact ``[Used {tool_name} on {project_id}]`` summary.
    """
    if not messages:
        return messages

    # Count user/assistant turns from the end to find the cutoff index.
    # Messages *after* the cutoff are "recent" and kept verbatim.
    # If the conversation is shorter than the window, keep everything.
    turn_count = 0
    cutoff_idx = 0  # Default: nothing is old, keep everything
    for i in range(len(messages) - 1, -1, -1):
        role = messages[i].get("role")
        if role in ("user", "assistant"):
            turn_count += 1
            if turn_count > recent_turns * 2:  # user+assistant = 2 msgs per turn
                cutoff_idx = i
                break

    result = []
    for i, msg in enumerate(messages):
        if i < cutoff_idx and msg.get("role") == "tool":
            result.append({
                **msg,
                "content": compact_tool_result(msg),
            })
        else:
            result.append(msg)
    return result


def compress_conversation(
    messages: list[dict],
    *,
    threshold: int = _SUMMARY_THRESHOLD,
    llm_summarize: callable | None = None,
) -> list[dict]:
    """Apply sliding-window summary when conversation exceeds threshold (Layer 2).

    If ``len(messages) > threshold``, the oldest messages are summarized into
    a single ``<conversation_summary>`` block and prepended to the recent
    messages.  Requires ``llm_summarize`` callable; if not provided, just
    truncates to the most recent ``threshold`` messages.

    Args:
        messages: Full conversation (excluding system prompt).
        threshold: Max messages before summarization kicks in.
        llm_summarize: Optional ``fn(text) -> summary_str`` that calls the LLM.
    """
    if len(messages) <= threshold:
        return messages

    keep_count = threshold
    old_messages = messages[: len(messages) - keep_count]
    recent_messages = messages[len(messages) - keep_count:]

    if llm_summarize is None:
        # No LLM available — just truncate with identity recovery
        logger.debug("Truncating conversation from %d to %d messages (no summarizer)", len(messages), keep_count)
        return _prepend_identity(recent_messages)

    # Format old messages for summarization
    formatted = _format_messages_for_summary(old_messages)
    try:
        summary = llm_summarize(formatted)
    except Exception:
        logger.warning("Conversation summarization failed; falling back to truncation")
        return _prepend_identity(recent_messages)

    if not summary:
        return _prepend_identity(recent_messages)

    summary_msg = {
        "role": "system",
        "content": f"<conversation_summary>\n{summary}\n</conversation_summary>",
    }
    return _prepend_identity([summary_msg] + recent_messages)


def _prepend_identity(messages: list[dict]) -> list[dict]:
    """Prepend identity block + acknowledgment after compression (P5).

    Prevents persona loss when older context is summarized or truncated.
    """
    return [
        {"role": "user", "content": _IDENTITY_BLOCK},
        {"role": "assistant", "content": "Understood. Continuing as AgentFIXBPro."},
    ] + messages


def _format_messages_for_summary(messages: list[dict]) -> str:
    """Format messages into readable text for summarization."""
    lines = []
    for msg in messages:
        role = msg.get("role", "unknown")
        content = msg.get("content", "")
        if role == "tool":
            name = msg.get("name", "tool")
            lines.append(f"[Tool: {name}] {content[:200]}")
        elif content:
            lines.append(f"{role.title()}: {content[:300]}")
    return "\n".join(lines)
