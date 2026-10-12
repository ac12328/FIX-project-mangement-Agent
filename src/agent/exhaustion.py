"""Deterministic exhaustion replies before optional LLM polish."""

from __future__ import annotations

import json
import re
from typing import Any

from src.agent.tool_receipts import ToolExecutionReceipt

_READ_TOOLS = frozenset({
    "get_project_status", "get_project_truth", "query_projects",
    "load_project_context", "search_knowledge", "wiki_search",
})

_WRITE_STAGE_TOOLS = frozenset({"update_project", "update_project_batch"})
_WRITE_CONFIRM_TOOLS = frozenset({"confirm_update", "cancel_update"})
_DELEGATE_TOOLS = frozenset({"delegate"})


def _flatten_tool_history(tool_history: list[tuple[str, ...]]) -> list[str]:
    return [name for rnd in tool_history for name in rnd]


def _snapshot_from_tool_content(content: str, tool_name: str = "") -> str | None:
    """Extract readable snapshot from raw or micro-compacted tool JSON."""
    if not content or not str(content).strip():
        return None
    try:
        parsed = json.loads(content)
    except (json.JSONDecodeError, TypeError):
        return content[:600] if content.strip() else None

    if not isinstance(parsed, dict):
        return str(parsed)[:600]

    # micro-compact digest
    if "tool" in parsed and "text" in parsed:
        text = (parsed.get("text") or "").strip()
        ids = parsed.get("ids") or {}
        if text:
            return text[:600]
        if ids:
            return json.dumps(ids, ensure_ascii=False)[:600]
        return None

    # get_project_status style
    if parsed.get("text"):
        return str(parsed["text"])[:600]

    # get_project_truth style
    project = parsed.get("project")
    if isinstance(project, dict) and project:
        pid = project.get("project_id") or ""
        parts = [f"**{pid}**"] if pid else []
        for k in ("project_status", "current_milestone", "ball_with", "next_action"):
            if project.get(k) not in (None, ""):
                parts.append(f"{k}={project[k]}")
        if parts:
            return " | ".join(parts)[:600]

    # ToolDigest
    if parsed.get("summary"):
        return str(parsed["summary"])[:600]

    # write-style free text embedded
    if parsed.get("message"):
        return str(parsed["message"])[:600]

    return None


def _last_read_snapshot(messages: list[dict]) -> str | None:
    # Resolve tool_call_id -> function.name from assistant messages
    id_to_name: dict[str, str] = {}
    for msg in messages:
        if msg.get("role") != "assistant":
            continue
        for tc in (msg.get("tool_calls") or []):
            fn = tc.get("function") or {}
            tc_id = tc.get("id")
            if tc_id and fn.get("name"):
                id_to_name[tc_id] = fn["name"]

    # Prefer richest recent tool result (truth/status/write success text)
    for msg in reversed(messages):
        if msg.get("role") != "tool":
            continue
        name = msg.get("name", "") or id_to_name.get(msg.get("tool_call_id", ""), "")
        content = msg.get("content") or ""
        snap = _snapshot_from_tool_content(content, name)
        if snap:
            return snap
        # Even write tools can be useful in exhaustion summary
        if name in _READ_TOOLS | _WRITE_STAGE_TOOLS | _WRITE_CONFIRM_TOOLS | _DELEGATE_TOOLS:
            if content.strip() and not snap:
                # try raw truncated
                if "Updated" in content or "M8" in content or "milestone" in content.lower():
                    return content[:600]
    return None


def _scan_write_success(messages: list[dict]) -> str | None:
    """Detect successful truth write wording in tool results (host or specialist)."""
    for msg in reversed(messages):
        if msg.get("role") != "tool":
            continue
        content = msg.get("content") or ""
        if "Updated " in content and (":" in content or "→" in content):
            return content[:400]
        try:
            parsed = json.loads(content)
        except (json.JSONDecodeError, TypeError):
            continue
        if not isinstance(parsed, dict):
            continue
        text = str(parsed.get("text") or parsed.get("summary") or "")
        if "Updated " in text:
            return text[:400]
        # nested specialist digest
        if "M8_GoLive" in content or "current_milestone" in content:
            snap = _snapshot_from_tool_content(content)
            if snap and ("M8" in snap or "Updated" in snap or "milestone" in snap):
                return snap
    return None


def _flatten_receipts(
    receipts: list[ToolExecutionReceipt],
) -> list[ToolExecutionReceipt]:
    flattened: list[ToolExecutionReceipt] = []
    for receipt in receipts:
        flattened.append(receipt)
        flattened.extend(_flatten_receipts(list(receipt.child_receipts)))
    return flattened


def _delegate_receipt_state(
    receipts: list[ToolExecutionReceipt],
) -> tuple[str, ToolExecutionReceipt] | None:
    delegates = [receipt for receipt in receipts if receipt.tool_name == "delegate"]
    if not delegates:
        return None
    delegate = delegates[-1]
    children = _flatten_receipts(list(delegate.child_receipts))
    committed = [
        receipt for receipt in children
        if receipt.status == "succeeded" and receipt.committed
    ]
    if delegate.committed or committed:
        return "committed", committed[-1] if committed else delegate
    staged = [
        receipt for receipt in children
        if receipt.status == "succeeded" and receipt.outcome == "staged"
    ]
    if delegate.outcome == "staged" or staged:
        return "staged", staged[-1] if staged else delegate
    if delegate.status in {"failed", "denied"}:
        return "failed", delegate
    if delegate.outcome == "exhausted":
        return "exhausted", delegate
    return "unverified", delegate


def _receipt_detail(receipt: ToolExecutionReceipt) -> str:
    parts = []
    if receipt.project_id:
        parts.append(receipt.project_id)
    parts.append(f"outcome={receipt.outcome}")
    if receipt.operation_id:
        parts.append(f"operation_id={receipt.operation_id}")
    if receipt.error_code:
        parts.append(f"error={receipt.error_code}")
    return " | ".join(parts)


def infer_incomplete_steps(tool_history: list[tuple[str, ...]]) -> list[str]:
    tools = _flatten_tool_history(tool_history)
    steps: list[str] = []
    if ("update_project" in tools or "update_project_batch" in tools) and "confirm_update" not in tools:
        steps.append("已暫存更新，尚未 confirm_update 確認。")
    if tools and tools[-1] in _WRITE_STAGE_TOOLS:
        steps.append("最後一輪仍在寫入流程中。")
    if "delegate" in tools and not any(
        t in tools for t in ("update_project", "update_project_batch", "confirm_update")
    ):
        steps.append("delegate 未回傳可驗證的 committed write receipt；未確認有資料寫入。")
    if not steps:
        steps.append("尚未產生最終回覆；請縮小範圍或回覆「繼續」。")
    return steps


def build_exhaustion_reply(
    messages: list[dict],
    tool_history: list[tuple[str, ...]],
    *,
    max_rounds: int,
    execution_receipts: list[ToolExecutionReceipt] | None = None,
) -> str:
    partial = _last_read_snapshot(messages)
    receipt_state = _delegate_receipt_state(execution_receipts or [])

    if receipt_state:
        state, receipt = receipt_state
        detail = _receipt_detail(receipt)
        if state == "committed":
            mastered = partial or detail
            return "\n".join([
                "✅ 寫入已完成。",
                "",
                "**已確認：**",
                mastered,
                "",
                "delegate 已回傳 committed write receipt；此操作不需要任何額外確認。",
            ])
        if state == "staged":
            return "\n".join([
                f"⚠️ 已達處理上限（{max_rounds}/{max_rounds} 輪）。",
                "",
                "**目前狀態：**",
                partial or detail,
                "",
                "**尚未完成：**",
                f"- 更新只完成 staging，尚未 commit（{detail}）。",
                "- 請確認這個 staged operation；不要把它當成已更新。",
            ])
        if state in {"failed", "exhausted"}:
            label = "specialist 已耗盡處理輪數" if state == "exhausted" else "specialist 寫入失敗"
            return "\n".join([
                f"⚠️ 已達處理上限（{max_rounds}/{max_rounds} 輪）。",
                "",
                "**目前狀態：**",
                partial or detail,
                "",
                "**尚未完成：**",
                f"- {label}（{detail}）。",
                "- 未有 committed write receipt，因此沒有確認任何資料更新。",
            ])

    remainder = infer_incomplete_steps(tool_history)
    mastered = partial or "（無可用讀取結果）"
    lines = [
        f"⚠️ 已達處理上限（{max_rounds}/{max_rounds} 輪）。",
        "",
        "**目前掌握：**",
        mastered,
        "",
        "**尚未完成：**",
        *[f"- {s}" for s in remainder],
        "",
        "請縮小範圍或重試；未有 committed receipt 前，不會視為已更新。",
    ]
    return "\n".join(lines)
