"""
Tool-call recovery from LLM text output.

Some local LLMs (e.g. Qwen, minimax) emit tool calls as XML or JSON
code blocks in their text response instead of using the structured
tool_calls API.  This module parses those patterns and executes them.
"""

from __future__ import annotations

import json
import logging
import re
import sqlite3

from src.observability.progress import ProgressCallback, emit_progress
from src.tools.registry import get_tools_for_context

logger = logging.getLogger(__name__)

# ── XML tool-call recovery ──────────────────────────────────────────────
_XML_TOOL_CALL_RE = re.compile(
    r"<tool_call>\s*<function=(\w+)>(.*?)</function>\s*</tool_call>",
    re.DOTALL,
)
_XML_PARAM_RE = re.compile(r"<parameter=(\w+)>\s*(.*?)\s*</parameter>", re.DOTALL)

# ── JSON code-block tool-call recovery ──────────────────────────────────
_JSON_TOOL_BLOCK_RE = re.compile(r"```(?:json)?\s*(.*?)\s*```", re.DOTALL)

# Known tool names for validation — derived from registry
_KNOWN_TOOLS = frozenset(t.name for t in get_tools_for_context("chat"))


def parse_xml_tool_calls(content: str) -> list[dict] | None:
    """Extract tool calls from <tool_call> XML in LLM text output.

    Returns list of {name, args} dicts, or None if no XML tool calls found.
    """
    matches = _XML_TOOL_CALL_RE.findall(content)
    if not matches:
        return None
    calls = []
    for fn_name, fn_body in matches:
        args = {}
        for param_name, param_value in _XML_PARAM_RE.findall(fn_body):
            stripped = param_value.strip()
            if stripped.startswith(("[", "{")):
                try:
                    args[param_name] = json.loads(stripped)
                    continue
                except json.JSONDecodeError:
                    pass
            args[param_name] = stripped
        calls.append({"name": fn_name, "args": args})
    return calls


def parse_json_block_tool_calls(content: str) -> list[dict] | None:
    """Extract tool calls from markdown JSON code blocks or bare JSON in LLM text.

    Handles fenced blocks (```json ... ```) and bare JSON objects containing
    a known tool name.  Returns list of {name, args} dicts, or None.
    """
    candidates: list[str] = []
    for m in _JSON_TOOL_BLOCK_RE.finditer(content):
        raw = m.group(1).strip()
        if raw.startswith("{"):
            candidates.append(raw)
    if not candidates:
        for m in re.finditer(r"\{", content):
            start = m.start()
            depth, i = 0, start
            while i < len(content):
                if content[i] == "{":
                    depth += 1
                elif content[i] == "}":
                    depth -= 1
                    if depth == 0:
                        raw = content[start : i + 1]
                        if '"tool"' in raw or '"function"' in raw or '"name"' in raw:
                            candidates.append(raw)
                        break
                i += 1
    if not candidates:
        return None
    calls = []
    for raw_json in candidates:
        try:
            obj = json.loads(raw_json)
        except json.JSONDecodeError:
            continue
        fn_name = obj.get("tool") or obj.get("function") or obj.get("name")
        fn_args = (
            obj.get("parameters") or obj.get("arguments")
            or obj.get("params") or {}
        )
        if fn_name and fn_name in _KNOWN_TOOLS and isinstance(fn_args, dict):
            if fn_name == "update_project_batch" and "updates" in fn_args and "changes" not in fn_args:
                updates_dict = fn_args.pop("updates")
                if isinstance(updates_dict, dict):
                    fn_args["changes"] = [
                        {"field": k, "new_value": str(v)} for k, v in updates_dict.items()
                    ]
            calls.append({"name": fn_name, "args": fn_args})
    return calls or None


def try_text_recovery(
    conn: sqlite3.Connection,
    content: str,
    execute_fn,
    *,
    progress_cb: ProgressCallback | None = None,
) -> str | None:
    """Attempt to recover tool calls from LLM text output (XML or JSON blocks).

    Returns the tool execution result string, or None if no recovery was possible.
    ``execute_fn`` is called as ``execute_fn(conn, name, args, progress_cb=cb)``.
    """
    recovered_calls = parse_xml_tool_calls(content) or parse_json_block_tool_calls(content)
    if not recovered_calls:
        return None

    logger.info("Recovered %d tool call(s) from LLM text output", len(recovered_calls))
    emit_progress(progress_cb, "Text recovery", "running",
                  f"Recovered {len(recovered_calls)} tool call(s) from text")
    tool_results = []
    for tc in recovered_calls:
        step_name = f"Execute tool: {tc['name']}"
        emit_progress(progress_cb, step_name, "running", "Running recovered tool call")
        try:
            result = execute_fn(conn, tc["name"], tc["args"], progress_cb=progress_cb)
        except Exception:
            logger.exception("Recovered tool call failed: %s", tc["name"])
            emit_progress(progress_cb, step_name, "error", "Tool execution failed")
            result = f"Error executing {tc['name']}"
        emit_progress(progress_cb, step_name, "done", "Tool returned a result")
        from src.agent.loop_action import LoopAction
        confirm_action = LoopAction.from_tool_result(result)
        if confirm_action is not None:
            import json as _json
            return _json.dumps(confirm_action.to_tool_dict())
        tool_results.append(f"**{tc['name']}**: {result}")
    return "\n\n".join(tool_results)
