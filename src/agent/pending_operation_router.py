"""Operation-bound routing for replies to staged project updates.

Pending write replies are resolved before generic portfolio reads so a status value
such as ``Waiting`` cannot be misclassified as a read filter while an owned staged
operation is awaiting confirmation.
"""

from __future__ import annotations

import logging
import re
import sqlite3
from dataclasses import dataclass
from typing import Callable, Literal

from src.agent.intent import detect_project_id
from src.models.enums import ProjectStatus
from src.models.pending_operation import PendingOperation

PendingReplyAction = Literal["confirm", "cancel", "clarify"]
logger = logging.getLogger(__name__)

_OPERATION_ID_RE = re.compile(r"\bop_[0-9a-f]{8,}\b", re.IGNORECASE)
_EXPLICIT_CONFIRM_RE = re.compile(
    r"\s*(?:confirm(?:_update)?|\u78ba\u8a8d)\s+"
    r"(?:operation\s*[:\uff1a]\s*)?(op_[0-9a-f]{8,})\s*[.!]?\s*",
    re.IGNORECASE,
)
_GENERIC_CONFIRM_RE = re.compile(
    r"\s*(?:yes|y|ok|okay|confirm|proceed|do it|go ahead|確認|可以|好|執行)\s*[.!]?\s*",
    re.IGNORECASE,
)
_GENERIC_CANCEL_RE = re.compile(
    r"\s*(?:no|cancel|stop|取消|不要|唔好)\s*[.!]?\s*",
    re.IGNORECASE,
)
_WRITE_VERB_RE = re.compile(
    r"\b(?:change|set|update|apply|confirm|proceed|use|select|choose)\b|"
    r"更改|改做|設定|套用|確認|選擇",
    re.IGNORECASE,
)
_QUERY_RE = re.compile(
    r"(?:^|\s)(?:what|which|show|list|find|how many|check|status of|details? of)\b|"
    r"[?？]|(?:查詢|查看|顯示|列出|有幾多|狀態係咩)",
    re.IGNORECASE,
)
_PENDING_PROMPT_RE = re.compile(
    r"staged|暫存|待確認|confirm_update|operation\s*[:：]",
    re.IGNORECASE,
)
_REPLY_CONTEXT_RE = re.compile(
    r'^\[Replying to bot message: ".*?"\]\s*\n\n',
    re.DOTALL,
)

_STATUS_BY_CASEFOLD = {status.value.casefold(): status.value for status in ProjectStatus}


@dataclass(frozen=True)
class PendingOperationReplyResult:
    """Deterministic result for a reply bound to one pending operation."""

    reply: str
    project_id: str
    operation_id: str
    action: PendingReplyAction
    committed: bool = False


def _operator_reply_text(user_text: str) -> str:
    """Remove Telegram reply-context injection before classifying the reply."""
    text = str(user_text or "").strip()
    match = _REPLY_CONTEXT_RE.match(text)
    if match:
        return text[match.end():].strip()
    return text


def _prompt_for_operation(
    conn: sqlite3.Connection,
    *,
    chat_id: str,
    operation: PendingOperation,
) -> str:
    """Return the newest outbound prompt tied to the operation, if available."""
    try:
        rows = conn.execute(
            """SELECT text FROM telegram_messages
               WHERE chat_id=? AND direction='outbound' AND created_at>=?
               ORDER BY created_at DESC, id DESC LIMIT 12""",
            (int(chat_id), operation.created_at),
        ).fetchall()
    except (sqlite3.Error, ValueError, TypeError):
        return ""

    project_id = str(operation.project_id or "")
    for row in rows:
        text = str(row["text"] if isinstance(row, sqlite3.Row) else row[0] or "")
        if operation.operation_id in text:
            return text
    for row in rows:
        text = str(row["text"] if isinstance(row, sqlite3.Row) else row[0] or "")
        if project_id and project_id in text and _PENDING_PROMPT_RE.search(text):
            return text
    return ""


def _prompt_status_options(prompt: str) -> dict[str, str]:
    """Parse numbered canonical project-status options from a staged prompt."""
    options: dict[str, str] = {}
    for raw_line in str(prompt or "").splitlines():
        line = re.sub(r"[*_`]", "", raw_line).strip()
        match = re.match(r"(\d+)\s*[.)]\s*([A-Za-z][A-Za-z_-]*)\b", line)
        if not match:
            continue
        canonical = _STATUS_BY_CASEFOLD.get(match.group(2).casefold())
        if canonical:
            options[match.group(1)] = canonical
    return options


def _requested_status(reply_text: str, prompt: str) -> str | None:
    normalized = re.sub(r"[*_`]", "", str(reply_text or ""))
    statuses = {
        canonical
        for token, canonical in _STATUS_BY_CASEFOLD.items()
        if re.search(rf"\b{re.escape(token)}\b", normalized, re.IGNORECASE)
    }
    if len(statuses) == 1:
        return next(iter(statuses))

    option_match = re.fullmatch(
        r"\s*(\d+)\s*[.)]?\s*(?:please|option)?\s*",
        normalized,
        re.IGNORECASE,
    )
    if option_match:
        return _prompt_status_options(prompt).get(option_match.group(1))
    return None


def _is_cancel_reply(reply_text: str) -> bool:
    if _GENERIC_CANCEL_RE.fullmatch(reply_text):
        return True
    if re.search(r"\bcancel_update\b", reply_text, re.IGNORECASE):
        return True
    return bool(
        re.search(r"取消|\bcancel\b", reply_text, re.IGNORECASE)
        and re.search(r"staged|暫存|update|operation", reply_text, re.IGNORECASE)
    )


def _is_confirm_reply(reply_text: str) -> bool:
    if _GENERIC_CONFIRM_RE.fullmatch(reply_text):
        return True
    if re.search(r"\bconfirm_update\b", reply_text, re.IGNORECASE):
        return True
    return bool(
        re.search(r"確認", reply_text)
        and re.search(r"staged|暫存|update|operation", reply_text, re.IGNORECASE)
    )


def _resolve_pending_operation(
    conn: sqlite3.Connection,
    user_text: str,
    *,
    chat_id: int,
    operator_id: str,
) -> PendingOperation | None:
    from src.agent.active_project import resolve_active_project_id
    from src.governance.four_eyes import is_high_risk_project_fields, team_four_eyes_enabled
    from src.repositories.pending_operation_repo import (
        get_operation,
        list_pending_operations,
        list_project_chat_pending_operations,
    )

    candidates = list_pending_operations(
        conn,
        operator_id=operator_id,
        chat_id=str(chat_id),
        operation_type="project_update",
    )
    # The operator body overrides IDs embedded in a quoted bot prompt.
    operation_match = (
        _OPERATION_ID_RE.search(_operator_reply_text(user_text))
        or _OPERATION_ID_RE.search(user_text or "")
    )
    if operation_match:
        operation_id = operation_match.group(0).lower()
        matches = [item for item in candidates if item.operation_id.lower() == operation_id]
        if len(matches) == 1:
            return matches[0]

        # A second reviewer may name a foreign operation, but never infer it
        # from a generic reply, a quoted bot prompt, or a cancellation.
        explicit_confirm = _EXPLICIT_CONFIRM_RE.fullmatch(_operator_reply_text(user_text))
        if not explicit_confirm or not team_four_eyes_enabled():
            return None
        operation = get_operation(conn, explicit_confirm.group(1).lower())
        if (operation is None or operation.operator_id == operator_id
                or str(operation.chat_id) != str(chat_id)
                or operation.operation_type != "project_update"):
            return None
        pending = list_project_chat_pending_operations(
            conn, chat_id=str(chat_id), project_id=operation.project_id,
        )
        if not any(item.operation_id == operation.operation_id for item in pending):
            return None
        high_risk, _ = is_high_risk_project_fields(
            conn, operation.project_id, (operation.payload or {}).get("fields") or {},
        )
        return operation if high_risk else None

    if not candidates:
        return None

    explicit_project_id = detect_project_id(user_text)
    if explicit_project_id:
        matches = [item for item in candidates if item.project_id == explicit_project_id]
        return matches[0] if len(matches) == 1 else None

    active_project_id = resolve_active_project_id(conn, chat_id)
    if active_project_id:
        matches = [item for item in candidates if item.project_id == active_project_id]
        if len(matches) == 1:
            return matches[0]

    return candidates[0] if len(candidates) == 1 else None


def _confirmation_summary(operation: PendingOperation, tool_result: dict) -> str:
    fields = operation.payload.get("fields") or {}
    lines = [
        f"已確認並套用 **{operation.project_id}** 嘅 staged update，DB 回讀完成。",
    ]
    labels = {
        "project_status": "Status",
        "current_milestone": "Milestone",
        "ball_with": "Ball",
        "next_action": "Next Action",
    }
    for field in ("project_status", "current_milestone", "ball_with", "next_action"):
        if field in fields:
            lines.append(f"• {labels[field]}：{fields[field]}")
    if "notes" in fields:
        lines.append("• Notes：已更新")
    lines.append(f"• Operation：`{operation.operation_id}`")
    if tool_result.get("outcome") == "no_change":
        lines[0] = f"**{operation.project_id}** 已經係相同狀態；operation 已確認並完成回讀。"
    return "\n".join(lines)


def _clarification_reply(
    operation: PendingOperation,
    *,
    requested_status: str | None,
) -> str:
    fields = operation.payload.get("fields") or {}
    staged_status = fields.get("project_status")
    if requested_status and staged_status and requested_status != staged_status:
        return (
            f"**{operation.project_id} 未有更新。** 目前 operation `{operation.operation_id}` "
            f"暫存嘅 Status 係 **{staged_status}**，但你回覆咗 **{requested_status}**。\n"
            "我唔會將呢個回覆當成專案查詢，亦唔會套用錯誤狀態。"
            f"請明確回覆「confirm {staged_status}」或者「cancel update」；"
            f"如要改為 {requested_status}，請明確要求重新暫存。"
        )
    target = f"Status → **{staged_status}**" if staged_status else "已暫存欄位"
    return (
        f"**{operation.project_id} 有一筆待確認更新**（{target}；"
        f"Operation `{operation.operation_id}`）。\n"
        "請回覆「confirm」、 「cancel update」，或者清楚講要改邊個值。"
    )


def try_pending_project_update_reply(
    conn: sqlite3.Connection | None,
    user_text: str,
    *,
    chat_id: int,
    operator_id: str | None = None,
    require_persisted_actor: bool = False,
    _owned_effects: list[Callable[[], None]] | None = None,
) -> PendingOperationReplyResult | None:
    """Resolve confirmation/cancellation before any generic typed-read route.

    Returns ``None`` for an ordinary read or unrelated message. A short or
    write-shaped reply bound to an owned pending operation is either executed or
    answered with a deterministic clarification; it is never sent to the
    portfolio query planner.
    """
    if conn is None:
        from src.db.connection import scoped_write

        effects: list[Callable[[], None]] = []
        with scoped_write() as wconn:
            result = try_pending_project_update_reply(
                wconn,
                user_text,
                chat_id=chat_id,
                operator_id=operator_id,
                require_persisted_actor=require_persisted_actor,
                _owned_effects=effects,
            )
        if wconn.in_transaction:
            raise RuntimeError("Staged-update write scope did not commit")
        for effect in effects:
            try:
                effect()
            except Exception:
                logger.exception("Committed pending-update refresh failed")
        return result

    from src.agent.operator_identity import resolve_operator_id
    from src.tools.executor import execute_tool

    effective_operator = operator_id or resolve_operator_id(conn, chat_id=chat_id)
    operation = _resolve_pending_operation(
        conn,
        user_text,
        chat_id=chat_id,
        operator_id=effective_operator,
    )
    if operation is None or not operation.project_id:
        return None

    reply_text = _operator_reply_text(user_text)
    prompt = _prompt_for_operation(
        conn,
        chat_id=str(chat_id),
        operation=operation,
    )
    requested_status = _requested_status(reply_text, prompt)
    staged_status = (operation.payload.get("fields") or {}).get("project_status")
    prompt_bound = bool(prompt)
    short_reply = len(reply_text) <= 160
    write_shaped = bool(_WRITE_VERB_RE.search(reply_text))

    if _QUERY_RE.search(reply_text) and not write_shaped:
        return None

    action: PendingReplyAction | None = None
    if _is_cancel_reply(reply_text):
        action = "cancel"
    elif _is_confirm_reply(reply_text) or _EXPLICIT_CONFIRM_RE.fullmatch(reply_text):
        action = "confirm"
    elif requested_status is not None:
        if requested_status == staged_status:
            action = "confirm"
        else:
            action = "clarify"
    elif prompt_bound and short_reply:
        action = "clarify"
    elif write_shaped and (detect_project_id(user_text) == operation.project_id):
        action = "clarify"

    if action is None:
        return None

    if action == "clarify":
        return PendingOperationReplyResult(
            reply=_clarification_reply(
                operation,
                requested_status=requested_status,
            ),
            project_id=operation.project_id,
            operation_id=operation.operation_id,
            action="clarify",
            committed=False,
        )

    if _owned_effects is None and conn.in_transaction:
        return PendingOperationReplyResult(
            reply=(
                f"**{operation.project_id} was not updated.** An existing transaction must "
                "finish before this confirmation or cancellation can be committed."
            ),
            project_id=operation.project_id,
            operation_id=operation.operation_id,
            action=action,
            committed=False,
        )

    from src.auth.rbac import P_BUSINESS_WRITE, permissions_for_role
    from src.auth.repository import get_user

    # Revalidate inside the writer: surface preflight may precede revocation.
    # Bound surface actors must not fall back to a trusted legacy namespace.
    # Direct legacy callers retain their existing compatibility contract.
    try:
        user = get_user(conn, effective_operator)
    except sqlite3.Error as exc:
        raise PermissionError("Cannot verify pending-update write permission") from exc
    if require_persisted_actor and (not operator_id or user is None):
        raise PermissionError("Pending-update actor no longer exists")
    execution_policy: dict[str, object] = {}
    if user is not None:
        permissions = (
            permissions_for_role(str(user["role"]))
            if user["status"] == "active" else frozenset()
        )
        execution_policy["dashboard_permissions"] = tuple(sorted(permissions))
        if P_BUSINESS_WRITE not in permissions:
            execution_policy["read_only_execution"] = True

    tool_name = "confirm_update" if action == "confirm" else "cancel_update"
    tool_result = execute_tool(
        conn,
        "*",
        tool_name,
        {
            "project_id": operation.project_id,
            "operation_id": operation.operation_id,
            "chat_id": str(chat_id),
            "_operator_id": effective_operator,
        },
        execution_context={
            **execution_policy,
            "operator_id": effective_operator,
            "chat_id": str(chat_id) if chat_id is not None else None,
            **({"owned_write_scope": True, "post_commit_effects": _owned_effects}
               if _owned_effects is not None else {}),
        },
    )
    if (
        isinstance(tool_result, dict)
        and tool_result.get("side_effect_status") == "may_have_occurred"
    ):
        raise RuntimeError(f"{tool_name} returned an uncertain write outcome")
    if not isinstance(tool_result, dict) or not tool_result.get("ok"):
        error_text = (
            str(tool_result.get("text") or tool_result.get("error") or "Update failed.")
            if isinstance(tool_result, dict)
            else str(tool_result)
        )
        return PendingOperationReplyResult(
            reply=(
                f"**{operation.project_id} 未有更新。** {error_text}\n"
                f"• Operation：`{operation.operation_id}`"
            ),
            project_id=operation.project_id,
            operation_id=operation.operation_id,
            action=action,
            committed=False,
        )

    if action == "cancel":
        reply = (
            f"已取消 **{operation.project_id}** 嘅 staged update；資料庫專案內容冇變。\n"
            f"• Operation：`{operation.operation_id}`"
        )
    else:
        reply = _confirmation_summary(operation, tool_result)
    return PendingOperationReplyResult(
        reply=reply,
        project_id=operation.project_id,
        operation_id=operation.operation_id,
        action=action,
        committed=bool(tool_result.get("committed")) or _owned_effects is not None,
    )


__all__ = [
    "PendingOperationReplyResult",
    "try_pending_project_update_reply",
]
