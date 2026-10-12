"""Per-loop replay cache for semantically identical successful read calls."""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Any

from src.agent.tool_receipts import ToolExecutionReceipt
from src.tools.registry import TOOL_BY_NAME


_READ_LIKE_EFFECTS = frozenset({"none", "read"})


@dataclass(frozen=True)
class CachedToolResult:
    """Tool-role content plus its trusted receipt from a prior loop round."""

    content: str
    receipt: ToolExecutionReceipt
    name: str | None = None


def is_cacheable_tool(tool_name: str) -> bool:
    """Limit replay to registered read-only or side-effect-free tools."""
    contract = TOOL_BY_NAME.get(tool_name)
    return bool(contract and contract.side_effect_type in _READ_LIKE_EFFECTS)


def replay_cached_result(
    cached: CachedToolResult,
    tool_call: dict[str, Any],
) -> tuple[dict[str, Any], ToolExecutionReceipt]:
    """Clone cached content and evidence under the model's new tool-call ID."""
    tool_call_id = str(tool_call.get("id") or "")
    tool_name = str(tool_call.get("function", {}).get("name") or "")
    message = {
        "role": "tool",
        "tool_call_id": tool_call_id,
        "name": cached.name or tool_name,
        "content": cached.content,
    }
    receipt = replace(
        cached.receipt,
        tool_call_id=tool_call_id,
        tool_name=tool_name or cached.receipt.tool_name,
        outcome="cached",
        cache_hit=True,
        execution_started=True,
    )
    return message, receipt


def remember_successful_result(
    cache: dict[str, CachedToolResult],
    *,
    signature: str,
    tool_call: dict[str, Any],
    message: dict[str, Any],
    receipt: ToolExecutionReceipt,
) -> None:
    """Cache only successful read-like results; failures and writes never enter."""
    tool_name = str(tool_call.get("function", {}).get("name") or "")
    if receipt.status != "succeeded" or not is_cacheable_tool(tool_name):
        return
    content = message.get("content")
    if not isinstance(content, str):
        return
    cache[signature] = CachedToolResult(
        content=content,
        receipt=receipt,
        name=str(message.get("name") or tool_name) or None,
    )
