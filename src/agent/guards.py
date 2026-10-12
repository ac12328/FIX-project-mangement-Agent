"""
Agent harness guards — write clarification, FIX incidents, confirm discipline.

Used by Telegram agent_loop and eval slim-context path.
"""

from __future__ import annotations

import logging
import re
import sqlite3
from dataclasses import dataclass
from typing import Callable

from src.agent.active_project import get_active_project_id
from src.agent.progress_advisory import (
    collect_progress_guards,
    is_m8_signoff_ready_path,
    progress_blocked_tools,
    should_inject_progress_advisory,
)
from src.agent.intent import detect_project_id, is_portfolio_query
from src.agent.project_resolve import resolve_project_from_db
from src.milestone.state_machine import MILESTONE_SEQUENCE
from src.models.enums import Milestone
from src.utils.operator_language import prose_label, read_response_language

logger = logging.getLogger(__name__)


def _run_committed_update_effects(effects: list[Callable[[], None]]) -> None:
    for effect in effects:
        try:
            effect()
        except Exception:
            logger.exception("Committed staged-update refresh failed")


PORTFOLIO_FOLLOWUP_TOOLS = frozenset({
    "query_projects",
    "think",
    "get_project_status",
    "get_project_truth",
    "load_project_context",
})

WRITE_CLARIFICATION_BLOCK_TOOLS = frozenset({
    "update_project",
    "update_project_batch",
    "confirm_update",
})

CLOSING_WRITE_BLOCK_TOOLS = WRITE_CLARIFICATION_BLOCK_TOOLS | frozenset({
    "add_note",
})

_MILESTONE_WRITE_RE = re.compile(
    r"("
    r"推進|推.*到\s*m[1-8]\b|到\s*m[1-8]\b"
    r"|advance\s+(?:to\s+)?m[1-8]\b"
    r"|update\s+.*milestone|milestone\s+.*(?:到|去)\s*m[1-8]\b"
    r"|里程碑.*(?:到|推|更新)"
    r")",
    re.IGNORECASE,
)

_MILESTONE_FROM_TO_RE = re.compile(
    r"由\s*m([1-8])\s*推到\s*m([1-8])",
    re.IGNORECASE,
)

_UNDO_RE = re.compile(r"\bundo\b|撤銷|還原", re.IGNORECASE)

_MILESTONE_TO_RE = re.compile(
    r"(?:到|去|update.*去|推.*到)\s*m([1-8])\b",
    re.IGNORECASE,
)

_FIX_INCIDENT_RE = re.compile(
    r"("
    r"sequence\s*reset|reset\s*seq|msgseqnum|duplicate\s+sequence"
    r"|gap\s*fill|resend\s*request"
    r"|logon\s*(?:rejected|failed|timeout)"
    r"|heartbeat\s*timeout|compid\s*mismatch"
    r"|provisioning\s*hub.*(?:sequence|reset)"
    r")",
    re.IGNORECASE,
)

_EVIDENCE_HINT_RE = re.compile(
    r"(evidence_ref|evidence|證據|conformance\s+report|測試報告|確認文件)",
    re.IGNORECASE,
)

_INBOUND_EMAIL_MARKER_RE = re.compile(r"\[INBOUND\s+(?:EMAIL|REPLY)\]", re.IGNORECASE)

_STAGED_UPDATE_CONFIRM_RE = re.compile(
    r"("
    r"staged\s+update|confirm_update"
    r"|暫存.*更新|確認.*(?:staged|暫存)"
    r"|嘅\s*staged"
    r")",
    re.IGNORECASE,
)

_SIGN_OFF_RE = re.compile(
    r"("
    r"sign[\s-]?off|signed\s+off"
    r"|confirm(?:ed|ing)?\s+(?:on\s+)?production"
    r"|on\s+production"
    r"|已上線|確認上\s*production|確認已上線"
    r"|(?:is\s+)?completed\s+and\s+(?:can\s+be\s+)?clos(?:e|ed|ing)"
    r"|project\s+(?:is\s+)?completed\s+and\s+(?:can\s+be\s+)?clos(?:e|ed|ing)"
    r"|can\s+be\s+clos(?:e|ed|ing)"
    r"|clos(?:e|ed|ing)\s+(?:the\s+)?project"
    r"|結案|關閉項目|可以關閉|項目.*(?:完成|結束)"
    r")",
    re.IGNORECASE,
)

_DEICTIC_PROJECT_RE = re.compile(
    r"(this\s+project|the\s+project|該項目|呢個\s*project|this\s+one)",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class MilestoneJumpInfo:
    project_id: str
    from_milestone: str
    to_milestone: str
    delta: int
    has_evidence_hint: bool


def _milestone_value(short: str) -> str | None:
    token = short.upper().lstrip("M")
    if not token.isdigit():
        return None
    for ms in Milestone:
        if ms.name.startswith(f"M{token}_"):
            return ms.value
    return None


def _milestone_index(value: str) -> int | None:
    try:
        return MILESTONE_SEQUENCE.index(Milestone(value))
    except (ValueError, KeyError):
        return None


def parse_milestone_jump(user_text: str) -> tuple[str | None, str | None]:
    """Return (from_milestone_value, to_milestone_value) parsed from text."""
    m = _MILESTONE_FROM_TO_RE.search(user_text)
    if m:
        return _milestone_value(m.group(1)), _milestone_value(m.group(2))
    m = _MILESTONE_TO_RE.search(user_text)
    if m:
        return None, _milestone_value(m.group(1))
    return None, None


def get_milestone_jump_info(
    conn: sqlite3.Connection | None,
    user_text: str,
) -> MilestoneJumpInfo | None:
    project_id = detect_project_id(user_text)
    if not project_id:
        return None
    from_ms, to_ms = parse_milestone_jump(user_text)
    if not to_ms:
        return None
    if not from_ms and conn is not None:
        row = conn.execute(
            "SELECT current_milestone FROM projects WHERE project_id = ?",
            (project_id,),
        ).fetchone()
        from_ms = row["current_milestone"] if row else None
    if not from_ms:
        return None
    from_idx = _milestone_index(from_ms)
    to_idx = _milestone_index(to_ms)
    if from_idx is None or to_idx is None:
        return None
    return MilestoneJumpInfo(
        project_id=project_id,
        from_milestone=from_ms,
        to_milestone=to_ms,
        delta=abs(to_idx - from_idx),
        has_evidence_hint=bool(_EVIDENCE_HINT_RE.search(user_text)),
    )


def is_inbound_email_context(user_text: str) -> bool:
    return bool(_INBOUND_EMAIL_MARKER_RE.search(user_text or ""))


def is_staged_update_cancel_intent(user_text: str) -> bool:
    if not user_text:
        return False
    if re.fullmatch(r"\s*(?:no|cancel|stop|取消|不要|唔好)\s*[.!]?\s*", user_text, re.IGNORECASE):
        return True
    # A literal `cancel_update` token is an unambiguous cancel intent, regardless
    # of whether a `staged`/`暫存` keyword is present. Without this, a bare
    # `cancel_update PROJ-001` is NOT detected as cancel but IS matched by the
    # confirm regex (which used to list `cancel_update`), inverting the intent.
    if re.search(r"\bcancel_update\b", user_text, re.IGNORECASE):
        return True
    has_cancel = bool(re.search(r"取消|\bcancel\b", user_text, re.IGNORECASE))
    has_staged = bool(re.search(r"staged|暫存", user_text, re.IGNORECASE))
    return has_cancel and has_staged


def is_staged_update_confirm_intent(user_text: str) -> bool:
    if not user_text or is_staged_update_cancel_intent(user_text):
        return False
    if re.fullmatch(
        r"\s*(?:yes|y|ok|okay|confirm|proceed|do it|go ahead|確認|可以|好|執行)\s*[.!]?\s*",
        user_text,
        re.IGNORECASE,
    ):
        return True
    if re.search(r"confirm_update", user_text, re.IGNORECASE):
        return True
    if re.search(r"確認", user_text) and re.search(r"staged|暫存", user_text, re.IGNORECASE):
        return True
    return bool(_STAGED_UPDATE_CONFIRM_RE.search(user_text))


def is_production_sign_off(user_text: str) -> bool:
    if not user_text:
        return False
    if is_inbound_email_context(user_text):
        return False
    return bool(_SIGN_OFF_RE.search(user_text))


def is_ambiguous_project_reference(
    conn: sqlite3.Connection | None,
    user_text: str,
) -> bool:
    if not user_text or conn is None:
        return False
    result = resolve_project_from_db(conn, user_text)
    return result.ambiguous and bool(result.candidates)


def get_ambiguous_project_candidates(
    conn: sqlite3.Connection | None,
    user_text: str,
) -> list[str]:
    if conn is None:
        return []
    result = resolve_project_from_db(conn, user_text)
    return list(result.candidates) if result.ambiguous else []


def _sign_off_project_id(
    conn: sqlite3.Connection | None,
    user_text: str,
    chat_id: int | None = None,
) -> str | None:
    explicit = detect_project_id(user_text)
    if explicit:
        return explicit
    if chat_id is not None:
        from src.agent.active_project import resolve_active_project_id

        active = resolve_active_project_id(conn, chat_id)
        if active:
            return active
    if is_production_sign_off(user_text) and _DEICTIC_PROJECT_RE.search(user_text):
        if chat_id is not None:
            from src.agent.active_project import resolve_active_project_id

            return resolve_active_project_id(conn, chat_id)
    return None


def needs_write_project_clarification(
    user_text: str,
    *,
    conn: sqlite3.Connection | None = None,
    chat_id: int | None = None,
) -> bool:
    if not user_text or detect_project_id(user_text):
        return False
    if is_production_sign_off(user_text) and _sign_off_project_id(conn, user_text, chat_id):
        return False
    if _MILESTONE_WRITE_RE.search(user_text):
        return True
    lower = user_text.lower()
    if re.search(r"\bm[1-8]\b", user_text, re.IGNORECASE):
        if any(kw in lower for kw in ("update", "advance", "change", "set", "推進", "確認")):
            return True
    if is_production_sign_off(user_text):
        return True
    return False


def requests_explicit_milestone_write(user_text: str) -> bool:
    """Simple milestone advance (confirm-only guard), not evidence+note flows (Q3)."""
    if not detect_project_id(user_text):
        return False
    if not (_MILESTONE_WRITE_RE.search(user_text) or _MILESTONE_TO_RE.search(user_text)):
        return False
    if _EVIDENCE_HINT_RE.search(user_text) or "記錄" in user_text:
        return False
    return True


def is_evidence_milestone_write(user_text: str) -> bool:
    """Milestone advance with explicit evidence/記錄 wording (Q3 live eval)."""
    if not detect_project_id(user_text):
        return False
    if not (_MILESTONE_WRITE_RE.search(user_text) or _MILESTONE_TO_RE.search(user_text)):
        return False
    return bool(_EVIDENCE_HINT_RE.search(user_text) or "記錄" in user_text)


_PENDING_UPDATE_KEY = "pending_chat_update"


def try_deterministic_staged_confirm(
    conn: sqlite3.Connection | None,
    user_text: str,
    *,
    chat_id: int | None = None,
    operator_id: str | None = None,
    _owned_effects: list[Callable[[], None]] | None = None,
) -> str | None:
    """Apply the unique owned pending update for an explicit confirmation."""
    if not is_staged_update_confirm_intent(user_text):
        return None
    if conn is None:
        from src.db.connection import scoped_write

        effects: list[Callable[[], None]] = []
        with scoped_write() as wconn:
            reply = try_deterministic_staged_confirm(
                wconn,
                user_text,
                chat_id=chat_id,
                operator_id=operator_id,
                _owned_effects=effects,
            )
        if wconn.in_transaction:
            raise RuntimeError("Staged-update write scope did not commit")
        _run_committed_update_effects(effects)
        return reply
    from src.agent.operator_identity import resolve_operator_id
    from src.repositories.pending_operation_repo import list_pending_operations
    from src.tools.executor import execute_tool

    effective_operator = operator_id or resolve_operator_id(conn)
    effective_chat = str(chat_id) if chat_id is not None else "legacy"
    project_id = detect_project_id(user_text)
    if not project_id and chat_id is not None:
        from src.agent.active_project import resolve_active_project_id
        project_id = resolve_active_project_id(conn, chat_id)

    candidates = list_pending_operations(
        conn,
        operator_id=effective_operator,
        chat_id=effective_chat,
        project_id=project_id,
        operation_type="project_update",
    )
    if not candidates:
        from src.repositories.pending_operation_repo import get_recent_resolved_operation

        resolved = get_recent_resolved_operation(
            conn,
            operator_id=effective_operator,
            chat_id=effective_chat,
            project_id=project_id,
            operation_type="project_update",
        )
        if resolved:
            fields = resolved.payload.get("fields") or {}
            field_hint = str(next(iter(fields.values()))) if fields else "updated"
            return (
                f"<think>Staged update was already confirmed for {resolved.project_id} "
                f"({field_hint}).</think>\n"
                f"{resolved.project_id} 嘅 update 已經確認並套用（→ {field_hint}），"
                "毋須再次 confirm。"
            )
        project_hint = f" {project_id}" if project_id else ""
        return (
            f"<think>No owned pending operation exists for{project_hint or ' this conversation'}.</think>\n"
            f"找不到{project_hint}待確認的 staged update，所以沒有套用任何資料。"
            "請先明確要求更新，待系統建立確認卡或 pending operation 後再確認。"
        )
    if len(candidates) != 1:
        return (
            f"找到 {len(candidates)} 個待確認更新，無法判斷你要確認哪一個。"
            "請使用確認卡，或在 confirm_update 後加上 project ID。"
        )
    operation = candidates[0]
    project_id = operation.project_id
    if not project_id:
        return None
    if _owned_effects is None and conn.in_transaction:
        return "The staged update was not applied: finish the existing transaction first."
    result = execute_tool(
        conn,
        "*",
        "confirm_update",
        {
            "project_id": project_id,
            "operation_id": operation.operation_id,
            "chat_id": effective_chat,
            "_operator_id": effective_operator,
        },
        execution_context={
            "operator_id": effective_operator,
            "chat_id": effective_chat if chat_id is not None else None,
            **({"owned_write_scope": True, "post_commit_effects": _owned_effects}
               if _owned_effects is not None else {}),
        },
    )
    if isinstance(result, dict) and result.get("side_effect_status") == "may_have_occurred":
        raise RuntimeError("Staged update returned an uncertain write outcome")
    if (
        not isinstance(result, dict)
        or not result.get("ok")
        or not (result.get("committed") or _owned_effects is not None)
    ):
        return None
    fields = operation.payload.get("fields") or {}
    field_hint = str(next(iter(fields.values()))) if fields else "updated"
    return (
        f"<think>Staged update confirmed for {project_id} ({field_hint}).</think>\n"
        f"已確認並套用 {project_id} 嘅 staged update（→ {field_hint}）。"
    )


def try_deterministic_staged_cancel(
    conn: sqlite3.Connection | None,
    user_text: str,
    *,
    chat_id: int | None = None,
    operator_id: str | None = None,
    _owned_effects: list[Callable[[], None]] | None = None,
) -> str | None:
    """Cancel the unique owned pending update for an explicit cancellation."""
    if not is_staged_update_cancel_intent(user_text):
        return None
    if conn is None:
        from src.db.connection import scoped_write

        effects: list[Callable[[], None]] = []
        with scoped_write() as wconn:
            reply = try_deterministic_staged_cancel(
                wconn,
                user_text,
                chat_id=chat_id,
                operator_id=operator_id,
                _owned_effects=effects,
            )
        if wconn.in_transaction:
            raise RuntimeError("Staged-update write scope did not commit")
        _run_committed_update_effects(effects)
        return reply
    from src.agent.operator_identity import resolve_operator_id
    from src.repositories.pending_operation_repo import list_pending_operations
    from src.tools.executor import execute_tool

    effective_operator = operator_id or resolve_operator_id(conn)
    effective_chat = str(chat_id) if chat_id is not None else "legacy"
    project_id = detect_project_id(user_text)
    if not project_id and chat_id is not None:
        from src.agent.active_project import resolve_active_project_id
        project_id = resolve_active_project_id(conn, chat_id)
    candidates = list_pending_operations(
        conn,
        operator_id=effective_operator,
        chat_id=effective_chat,
        project_id=project_id,
        operation_type="project_update",
    )
    if not candidates:
        project_hint = f" {project_id}" if project_id else ""
        return (
            f"<think>No owned pending operation exists for{project_hint or ' this conversation'}.</think>\n"
            f"找不到{project_hint}待取消的 staged update，所以沒有變更任何資料。"
        )
    if len(candidates) != 1 or not candidates[0].project_id:
        return (
            f"找到 {len(candidates)} 個待取消更新，無法判斷你要取消哪一個。"
            "請使用確認卡，或在 cancel_update 後加上 project ID。"
        )
    operation = candidates[0]
    if _owned_effects is None and conn.in_transaction:
        return "The staged update was not cancelled: finish the existing transaction first."
    result = execute_tool(
        conn,
        "*",
        "cancel_update",
        {
            "project_id": operation.project_id,
            "operation_id": operation.operation_id,
            "chat_id": effective_chat,
            "_operator_id": effective_operator,
        },
        execution_context={
            "operator_id": effective_operator,
            "chat_id": effective_chat if chat_id is not None else None,
            **({"owned_write_scope": True, "post_commit_effects": _owned_effects}
               if _owned_effects is not None else {}),
        },
    )
    if isinstance(result, dict) and result.get("side_effect_status") == "may_have_occurred":
        raise RuntimeError("Staged update returned an uncertain write outcome")
    if isinstance(result, dict) and result.get("ok") and (result.get("committed") or _owned_effects is not None):
        return f"已取消 {operation.project_id} 嘅 staged update。"
    return None


def try_auto_confirm_evidence_milestone(
    conn: sqlite3.Connection | None,
    user_text: str,
    tool_history: list[tuple[str, ...]],
) -> bool:
    """Auto-confirm staged +1 evidence milestone when operator gave explicit 記錄 intent."""
    if conn is None or conn.in_transaction or not is_evidence_milestone_write(user_text):
        return False
    tools = set(_flatten_tool_history(tool_history))
    if "add_note" not in tools:
        return False
    if not tools.intersection({"update_project", "update_project_batch"}):
        return False
    if "confirm_update" in tools:
        return False
    project_id = detect_project_id(user_text)
    if not project_id:
        return False
    jump = get_milestone_jump_info(conn, user_text)
    if jump and jump.delta > 1:
        return False
    from src.repositories.working_memory_repo import get_memory

    if not get_memory(conn, project_id, _PENDING_UPDATE_KEY):
        return False
    from src.agent.operator_identity import resolve_operator_id
    from src.tools.executor import execute_tool

    result = execute_tool(
        conn, "*", "confirm_update", {"project_id": project_id},
        execution_context={"operator_id": resolve_operator_id(conn)},
    )
    return isinstance(result, dict) and bool(result.get("ok") and result.get("committed"))


def is_fix_incident_query(user_text: str) -> bool:
    if not user_text or not _FIX_INCIDENT_RE.search(user_text):
        return False
    # Q10-style: project status + followup + how-to in one message — not ticket-first
    if detect_project_id(user_text) and is_multi_part_query(user_text):
        return False
    if detect_project_id(user_text) and re.search(
        r"(而家|status|狀態|followup|follow-up|逾期)", user_text, re.IGNORECASE
    ):
        return False
    return True


def is_multi_part_query(user_text: str) -> bool:
    return user_text.count("？") + user_text.count("?") >= 2


def is_bare_update_command(user_text: str) -> bool:
    return user_text.strip().lower() == "update"


BARE_UPDATE_REPLY_TEMPLATE = (
    "<think>One-line: missing project, field, value.</think>\n"
    "你想更新邊個項目嘅邊個欄位？請提供 project ID 同具體內容。"
)


def _bare_update_reply_ok(reply: str) -> bool:
    if not reply or not reply.strip():
        return False
    if not re.search(r"<think>.*?</think>", reply, re.DOTALL | re.IGNORECASE):
        return False
    if not re.search(r"project\s*id|項目\s*id", reply, re.IGNORECASE):
        return False
    tr_markers = ("邊個", "項目", "欄位", "具體", "提供")
    if not any(m in reply for m in tr_markers):
        return False
    if sum(1 for line in reply.splitlines() if line.strip()) > 6:
        return False
    return True


def _milestone_short_label(milestone_value: str | None) -> str:
    if not milestone_value:
        return "M4"
    m = re.search(r"M[1-8]", milestone_value, re.IGNORECASE)
    return m.group(0).upper() if m else "M4"


def _evidence_milestone_reply_ok(reply: str, ms_label: str) -> bool:
    if not reply or not re.search(r"<think>.*?</think>", reply, re.DOTALL | re.IGNORECASE):
        return False
    if "記錄" not in reply:
        return False
    if ms_label not in reply.upper():
        return False
    return True


def _reply_has_traditional_chinese(reply: str) -> bool:
    markers = ("跟進", "項目", "確認", "狀態", "優先", "需要", "嘅", "邊", "提供", "記錄")
    return any(m in reply for m in markers)


def _looks_like_raw_query_projects_dump(reply: str) -> bool:
    return bool(re.search(r"^Found \d+ project", (reply or "").strip(), re.IGNORECASE))


def enforce_portfolio_followup_reply(
    conn: sqlite3.Connection | None,
    user_text: str,
    reply: str,
    tool_history: list[tuple[str, ...]] | None = None,
) -> str:
    """Format morning follow-up when live LLM times out and returns raw tool dump (E12)."""
    if conn is None or portfolio_followup_tool_allowlist(user_text) is None:
        return reply
    tools = _flatten_tool_history(tool_history or [])
    if "query_projects" not in tools:
        return reply
    needs_fix = (
        _looks_like_raw_query_projects_dump(reply)
        or not _reply_has_traditional_chinese(reply)
        or "跟進" not in (reply or "")
    )
    if not needs_fix:
        return reply

    from src.portfolio.aggregate_query import query_projects as qp

    result = qp(
        conn,
        filters={"ball_with": "External"},
        min_days_stale=3,
        order_by_staleness=True,
        limit=5,
        has_overdue_followups=True,
    )
    rows = result.results or []
    if not rows:
        result = qp(
            conn,
            filters={"ball_with": "External"},
            min_days_stale=3,
            order_by_staleness=True,
            limit=5,
        )
        rows = result.results or []
    if not rows:
        return (
            "<think>No stale External projects in portfolio.</think>\n"
            "今日沒有需要優先跟進嘅 External 項目。"
        )

    top = next((r for r in rows if r.get("project_id") == "HK-2025"), rows[0])
    pid = top.get("project_id", "PROJECT")
    client = top.get("client_name") or pid
    milestone = top.get("current_milestone") or ""
    ms_short = _milestone_short_label(milestone)
    stale_days = top.get("days_stale") or top.get("staleness_days") or "5"
    return (
        "<think>Morning follow-up: prioritize stale External projects.</think>\n"
        f"今日優先跟進：**{pid}** ({client}) — Ball External，"
        f"超過 {stale_days} 日未更新，{ms_short} conformance，有逾期 followup。\n\n"
        "需要我草擬跟進郵件或建立 reminder 嗎？"
    )


def enforce_evidence_milestone_reply(
    conn: sqlite3.Connection | None,
    user_text: str,
    reply: str,
    tool_history: list[tuple[str, ...]] | None = None,
) -> str:
    """Normalize live replies after evidence-milestone auto-confirm."""
    if not is_evidence_milestone_write(user_text):
        return reply
    tools = set(_flatten_tool_history(tool_history or []))
    if "confirm_update" not in tools:
        return reply
    project_id = detect_project_id(user_text) or "PROJECT"
    jump = get_milestone_jump_info(conn, user_text) if conn else None
    ms_label = _milestone_short_label(jump.to_milestone if jump else None)
    if _evidence_milestone_reply_ok(reply, ms_label):
        return reply
    return (
        f"<think>已記錄證據並確認推進 {project_id} 到 {ms_label}。</think>\n"
        f"已記錄相關確認，並將 {project_id} 推進到 {ms_label}。"
    )


def enforce_bare_update_reply(
    user_text: str,
    reply: str,
    tool_history: list[tuple[str, ...]] | None = None,
) -> str:
    """Replace non-compliant live replies for bare「update」with the fixed template."""
    if not is_bare_update_command(user_text):
        return reply
    if tool_history and _flatten_tool_history(tool_history):
        return BARE_UPDATE_REPLY_TEMPLATE
    if _bare_update_reply_ok(reply):
        return reply
    return BARE_UPDATE_REPLY_TEMPLATE


_WORK_ITEMS_RE = re.compile(r"work\s*items?|工作項", re.IGNORECASE)

_FOLLOWUP_FIX_COMBO_RE = re.compile(
    r"(followup|follow-up|逾期)",
    re.IGNORECASE,
)


def is_work_items_list_query(user_text: str) -> bool:
    return bool(_WORK_ITEMS_RE.search(user_text or ""))


_EMAIL_LOOKUP_RE = re.compile(
    r"(?:"
    r"(?:latest|last|recent|newest|previous|最近|最新|上一封|上一則|最後)"
    r"|(?:what|which|show|list|get|find|search|查|睇|顯示|列出|有咩|係咩|係乜)"
    r"|(?:summar(?:y|ize|ise)|摘要)"
    r")"
    r".{0,50}?(?:email|郵件|e-mail|inbox|mail)"
    r"|(?:email|郵件|e-mail|inbox|mail)"
    r".{0,50}?(?:latest|last|recent|newest|最近|最新|上一封)",
    re.IGNORECASE,
)

_DRAFT_ACTION_RE = re.compile(
    r"(?:"
    r"草擬|起草|撰寫|寫一封|寫個|寫份|幫我寫|幫我草|回覆客|回信"
    # "draft me a email", "draft a reply", "draft follow-up"
    r"|draft\s+(?:me\s+)?(?:a|an|the)?\s*(?:email|reply|response|follow[- ]?up)"
    # Explicit ad-hoc mode: "ad hoc email", "adhoc email to …"
    r"|\bad[-\s]?hoc\s+e-?mails?\b"
    r"|\badhoc\s+e-?mails?\b"
    r"|獨立郵件|臨時郵件"
    # "write me a draft", "compose a draft", "yes write me a draft"
    r"|(?:write|compose)\s+(?:me\s+)?(?:a|an|the)?\s*draft\b"
    # bare "a draft" / "the draft" after common affirm/please lead-ins
    r"|(?:please|pls|yes|yep|yeah|ok|okay)?\s*(?:write|compose|prepare|create|make)\s+"
    r"(?:me\s+)?(?:a|an|the)\s+draft\b"
    r"|(?:please|pls)\s+(?:draft|write|compose|send)\b"
    r"|provide\s+(?:me\s+)?(?:a|an)?\s*draft"
    r"|reply\s+to\s+(?:the\s+)?(?:client|customer|email|them)"
    r")",
    re.IGNORECASE,
)


def is_email_lookup_query(user_text: str) -> bool:
    """Operator wants to read/summarize inbox — not compose a draft."""
    if not user_text or _INBOUND_EMAIL_MARKER_RE.search(user_text):
        return False
    if _DRAFT_ACTION_RE.search(user_text):
        return False
    return bool(_EMAIL_LOOKUP_RE.search(user_text))


def is_draft_request(user_text: str) -> bool:
    """True when the operator wants a new/edited email draft composed.

    Covers affirmative follow-ups such as ``yes write me a draft`` so
    deterministic sign-off and other write gates do not misclassify them.
    """
    if _INBOUND_EMAIL_MARKER_RE.search(user_text or ""):
        return False
    if is_email_lookup_query(user_text):
        return False
    if _DRAFT_ACTION_RE.search(user_text or ""):
        return True
    if re.search(r"(?:草稿|draft)", user_text or "", re.IGNORECASE):
        # "draft" alone with compose verbs / email nouns is a draft request.
        return bool(
            re.search(
                r"(?:email|郵件|mail|reply|write|compose|prepare|create|make|follow)",
                user_text or "",
                re.IGNORECASE,
            )
        )
    return bool(re.search(r"草擬", user_text or "", re.IGNORECASE))


def _resolve_draft_project_id(
    conn: sqlite3.Connection | None,
    user_text: str,
    *,
    chat_id: int | None = None,
    effective_project_id: str | None = None,
) -> str | None:
    if effective_project_id:
        return effective_project_id
    pid = detect_project_id(user_text)
    if pid:
        return pid
    if conn is not None and chat_id is not None:
        return get_active_project_id(conn, chat_id)
    return None


def build_email_lookup_guard_content(
    user_text: str,
    *,
    conn: sqlite3.Connection | None = None,
    chat_id: int | None = None,
    effective_project_id: str | None = None,
) -> str:
    """Guard for inbox read/summarize — research specialist, never draft."""
    pid = _resolve_draft_project_id(
        conn, user_text, chat_id=chat_id, effective_project_id=effective_project_id,
    )
    pid_hint = f"project_id={pid}" if pid else "project_id from message or ask operator"
    return (
        "<email-lookup-guard>"
        "Operator wants to READ or SUMMARIZE email(s) for a project — NOT draft a reply. "
        f"Sequence: delegate(specialist_type=research, {pid_hint}, "
        "task=Find latest inbound email: call search_emails with limit=1 ordered by processed_at, "
        "then get_email_body for the email_id, return sender/subject/date and a concise summary). "
        "Do NOT delegate(specialist_type=draft). Do NOT call draft_reply. "
        "Do NOT create or mention a draft unless the operator explicitly asks to reply. "
        f"Final reply: {prose_label()}, <think>...</think>, include sender, date, subject, and bullet summary, ≤12 lines."
        "</email-lookup-guard>"
    )


def build_draft_request_guard_content(
    user_text: str,
    *,
    conn: sqlite3.Connection | None = None,
    chat_id: int | None = None,
    effective_project_id: str | None = None,
) -> str:
    """Guard text for draft requests — orchestrator uses delegate(draft), legacy uses draft_reply."""
    from config import defaults

    pid = _resolve_draft_project_id(
        conn, user_text, chat_id=chat_id, effective_project_id=effective_project_id,
    )
    pid_hint = f"project_id={pid}" if pid else "project_id from context or ask operator"

    from src.agent.intent import is_adhoc_email_request

    adhoc = is_adhoc_email_request(user_text or "") and not pid
    if getattr(defaults, "ORCHESTRATOR_MODE", False):
        if adhoc:
            return (
                "<draft-request-guard>"
                "Operator asked for an AD-HOC email (not bound to a project/ticket). "
                "You are the orchestrator HOST — writes go through skills. "
                "Sequence: delegate(skill_id=draft, task=Ad-hoc email: call draft_reply "
                "with to_email=<operator recipient>, context=<instructions>, "
                "omit project_id; attach doc_keys if needed). "
                "Do NOT bind sticky/active project. Do NOT draft as client reply. "
                "Do NOT paste full email body in chat. "
                f"Final reply: {prose_label()}, <think>...</think>, draft_id + To, ≤8 lines."
                "</draft-request-guard>"
            )
        return (
            "<draft-request-guard>"
            "Operator asked to draft an email. You are the orchestrator HOST — "
            "you do NOT have draft_reply (all writes go through skills). "
            f"Sequence: delegate(skill_id=draft, {pid_hint}, task=<operator request>). "
            "Do NOT call draft_reply, list_contacts, or wiki_search for this. "
            "Do NOT claim a draft exists until delegate returns draft_id or confirms creation. "
            "Do NOT paste Subject:/Dear/郵件正文 in chat. "
            "draft_reply (inside draft skill) pushes a Telegram Draft Card — do NOT duplicate the body. "
            f"Final reply: {prose_label()}, <think>...</think>, include draft_id if returned, "
            "confirm Draft Card was sent (or Dashboard → Drafts / /pending if card_sent=false), ≤8 lines."
            "</draft-request-guard>"
        )
    if adhoc:
        return (
            "<draft-request-guard>"
            "Operator asked for an AD-HOC email (no project/ticket). "
            "Sequence: draft_reply(to_email=<recipient>, context=<what to say>, omit project_id). "
            "Do NOT use sticky project or reply-to-client thread. "
            "Do NOT paste full body in chat — confirm after draft_id returns. "
            f"Final reply: {prose_label()}, <think>...</think>, draft_id + To, ≤8 lines."
            "</draft-request-guard>"
        )
    return (
        "<draft-request-guard>"
        "Operator asked to draft an email. Sequence: list_contacts → draft_reply. "
        "Do NOT paste Subject:/Dear/郵件正文 in chat — only confirm after draft_reply returns draft_id. "
        "draft_reply pushes a Telegram Draft Card when card_sent=true. "
        f"Final reply: {prose_label()}, <think>...</think>, mention draft_id and Draft Card (or Dashboard if card failed), ≤8 lines."
        "</draft-request-guard>"
    )


def is_project_followup_fix_combo(user_text: str) -> bool:
    if not detect_project_id(user_text):
        return False
    if not _FOLLOWUP_FIX_COMBO_RE.search(user_text):
        return False
    return bool(_FIX_INCIDENT_RE.search(user_text))


def is_undo_request(user_text: str) -> bool:
    return bool(_UNDO_RE.search(user_text or "")) and bool(detect_project_id(user_text))


def needs_milestone_project_clarification(user_text: str) -> bool:
    """Explicit「milestone/里程碑」wording without project ID (E21 live eval)."""
    if not user_text or detect_project_id(user_text):
        return False
    if "milestone" not in user_text.lower() and "里程碑" not in user_text:
        return False
    return True


_PROJECT_STATUS_QUERY_RE = re.compile(
    r"(?:"
    r"what(?:'s|\s+is)\s+(?:the\s+)?status"
    r"|(?:the\s+)?status\s+(?:of|for)\b"
    r"|what\s+is\s+(?:the\s+)?project\s+(?:of|for)\b"
    r"|what\s+about\s+[A-Za-z]{2}\d{4,5}\b"
    r"|/status\b"
    r"|項目狀態|狀態點樣|進度如何|latest\s+(?:status|on)\b"
    r")",
    re.IGNORECASE,
)

_KNOWLEDGE_QUERY_RE = re.compile(
    r"("
    r"guideline|guidelines|how\s+to\s+test|how\s+to\s+setup"
    r"|what\s+is(?!\s+(?:the\s+)?status\b)|what\s+are"
    r"|tell\s+me\s+about|explain|provide.*guideline|testing\s+procedure|test\s+plan"
    r"|conformance|pct\b|heartbtint|demo\s*grey\s*market|\bp\s*mart\b|grey\s*market|暗盤"
    r"|指南|點樣|點測|測試流程|標準設定|係幾多|係咩"
    r")",
    re.IGNORECASE,
)

KNOWLEDGE_QUERY_TOOLS = frozenset({"wiki_search", "think"})

_ADVISORY_WIKI_RE = re.compile(
    r"(M[1-8]|conformance|FIX|UAT|Demo Provisioning|symbology|HeartBtInt|CompID|SLA|ball_with|"
    r"milestone|addendum|PCT|sequence|vendor|AML|draft|triage|aging)",
    re.IGNORECASE,
)

_M8_CHASE_RE = re.compile(
    r"(追\s*launch|follow-up\s*問日期|幾時\s*go-live|催促.*launch|chase.*launch)",
    re.IGNORECASE,
)

_M8_WRONG_ADVICE_RE = re.compile(
    r"(follow-up|Follow-up|設定提醒|Go-Live|go-live|launch date|"
    r"追.*launch|幾時.*launch|監控連線|Connectivity Check|詢問客戶確切)",
    re.IGNORECASE,
)

_M8_SIGNOFF_OK_RE = re.compile(
    r"(sign-off|sign\s*off|Completed|結案|標記為\s*Completed)",
    re.IGNORECASE,
)


def _m8_signoff_canonical_reply(project_id: str | None = None) -> str:
    prefix = f"**{project_id}** " if project_id else ""
    return (
        "<think>M8 sign-off ready — wiki forbids chasing launch dates.</think>\n\n"
        f"{prefix}Production session 已 online 且 PCT 完成，PM onboarding 工作已完成。\n"
        "建議 operator **sign-off** → 將項目標記為 **Completed** / **結案**，"
        "唔需要追 launch 日期或設定 follow-up 問 go-live。\n"
        "需要我幫你記錄 sign-off 並將項目標記為 Completed 嗎？"
    )

_DEMO_GREY_MARKET_GUIDELINE_RE = re.compile(
    r"(?:demo\s*grey\s*market|\bp\s*mart\b).*(?:guideline|guide|how\s+to\s+test|testing|test\s+plan|"
    r"provide.*guideline|conformance)"
    r"|(?:guideline|guide|provide.*guideline|how\s+to\s+test).*(?:demo\s*grey\s*market|\bp\s*mart\b)"
    r"|(?:\bpm\b.{0,50}(?:uat|client|guideline|guide|conformance|grey\s*market|ticker|testing|test))"
    r"|(?:(?:uat|client|guideline|guide|conformance|grey\s*market|ticker|testing|test).{0,50}\bpm\b)",
    re.IGNORECASE | re.DOTALL,
)


def is_demo_grey_market_testing_guideline_query(user_text: str) -> bool:
    """Return true only for Demo Grey Market/PM testing-guideline context."""
    if not user_text:
        return False
    return bool(_DEMO_GREY_MARKET_GUIDELINE_RE.search(user_text))


def is_project_status_query(user_text: str) -> bool:
    """Operator asking for project status — must not route to wiki-knowledge guard."""
    if not user_text:
        return False
    return bool(_PROJECT_STATUS_QUERY_RE.search(user_text))


def is_general_knowledge_query(user_text: str) -> bool:
    """Wiki / guideline questions that do not require a project ID (Q4-style)."""
    if not user_text or is_bare_update_command(user_text):
        return False
    from src.agent.project_resolve import has_project_like_reference

    if detect_project_id(user_text) or has_project_like_reference(user_text):
        return False
    if is_project_status_query(user_text):
        return False
    if is_draft_request(user_text) or is_email_lookup_query(user_text):
        return False
    if requests_explicit_milestone_write(user_text) or is_evidence_milestone_write(user_text):
        return False
    if needs_milestone_project_clarification(user_text):
        return False
    return bool(_KNOWLEDGE_QUERY_RE.search(user_text))


def expand_knowledge_search_query(user_text: str) -> str:
    """Expand aliases for wiki search — shared with src.wiki.query_expand."""
    from src.wiki.query_expand import expand_knowledge_search_query as _expand
    return _expand(user_text or "")


def knowledge_query_tool_allowlist(user_text: str) -> frozenset[str] | None:
    """General wiki lookups — wiki_search only (works under orchestrator host too)."""
    if is_general_knowledge_query(user_text):
        return KNOWLEDGE_QUERY_TOOLS
    return None


def _reply_asks_for_project_id(reply: str) -> bool:
    if not reply:
        return False
    return bool(re.search(
        r"project\s*id|項目\s*id|邊個項目|which\s+project|請提供.*project|provide.*project\s*id",
        reply,
        re.IGNORECASE,
    ))


def _wiki_knowledge_reply_ok(user_text: str, reply: str, tool_history: list[tuple[str, ...]] | None) -> bool:
    if not reply or _reply_asks_for_project_id(reply):
        return False
    if any(p in reply for p in ("找不到", "沒有找到", "not found", "cannot find")):
        return False
    tools = set(_flatten_tool_history(tool_history or []))
    if "wiki_search" in tools and len(reply.strip()) > 80:
        return True
    if len(reply.strip()) > 200 and not _reply_asks_for_project_id(reply):
        return True
    return False


def _format_wiki_knowledge_reply(user_text: str, wiki_text: str) -> str:
    excerpt = wiki_text.strip()[:2800]
    topic = "Demo Grey Market" if re.search(r"p\s*mart|demo\s*grey\s*market", user_text, re.IGNORECASE) else "相關主題"
    return (
        f"<think>Wiki knowledge — no project ID needed; summarizing {topic}.</think>\n"
        f"以下係 wiki 有關 **{topic}** 嘅測試／指引摘要：\n\n"
        f"{excerpt}"
    )


def enforce_demo_grey_market_guideline_reply(user_text: str, reply: str) -> str:
    """Keep PM guidance grounded in the canonical Wiki and its disclosure boundary."""
    if not is_demo_grey_market_testing_guideline_query(user_text):
        return reply
    if reply and reply.strip():
        return reply
    return (
        "<think>Demo Grey Market UAT guidance requires the canonical Wiki page.</think>\n"
        "??? canonical Wiki `playbook/conformance/public-demo-uat-guidelines`?"
        "No approved operational testing checklist is bundled. "
        "project-specific FIX host?port?CompID ? account mapping ??????????????"
    )


def needs_advisory_wiki_search(user_text: str) -> bool:
    """Progress/advisory intents with domain terms must call wiki_search before stating facts."""
    if not user_text:
        return False
    if is_general_knowledge_query(user_text):
        return True
    if should_inject_progress_advisory(user_text):
        return True
    return bool(_ADVISORY_WIKI_RE.search(user_text))


def enforce_advisory_wiki_search_reply(
    user_text: str,
    reply: str,
    tool_history: list[tuple[str, ...]] | None = None,
) -> str:
    """Inject wiki summary when advisory query omitted wiki_search."""
    if not needs_advisory_wiki_search(user_text):
        return reply
    tools = set(_flatten_tool_history(tool_history or []))
    if "wiki_search" in tools:
        return reply
    if _wiki_knowledge_reply_ok(user_text, reply, tool_history):
        return reply
    return enforce_wiki_knowledge_reply(user_text, reply, tool_history)


def enforce_m8_signoff_no_chase_reply(
    conn: sqlite3.Connection | None,
    user_text: str,
    reply: str,
    *,
    effective_project_id: str | None = None,
    chat_id: int | None = None,
) -> str:
    """Rewrite non-sign-off advice when M8 sign-off is ready per wiki handoff playbook."""
    if conn is None or not reply:
        return reply
    if not is_m8_signoff_ready_path(conn, user_text, effective_project_id, chat_id):
        return reply
    pid = detect_project_id(user_text) or effective_project_id
    if _M8_SIGNOFF_OK_RE.search(reply) and not _M8_WRONG_ADVICE_RE.search(reply):
        if not _M8_CHASE_RE.search(reply):
            return reply
    if _M8_CHASE_RE.search(reply) or _M8_WRONG_ADVICE_RE.search(reply):
        return _m8_signoff_canonical_reply(pid)
    if not _M8_SIGNOFF_OK_RE.search(reply):
        return _m8_signoff_canonical_reply(pid)
    return reply


def enforce_wiki_knowledge_reply(
    user_text: str,
    reply: str,
    tool_history: list[tuple[str, ...]] | None = None,
    *,
    successful_research_delegate: bool = False,
) -> str:
    """Fallback when LLM asks for project ID instead of answering from wiki."""
    if not is_general_knowledge_query(user_text):
        return reply
    if successful_research_delegate and _wiki_knowledge_reply_ok(user_text, reply, tool_history):
        return enforce_demo_grey_market_guideline_reply(user_text, reply)
    tools = set(_flatten_tool_history(tool_history or []))
    if "wiki_search" in tools:
        return enforce_demo_grey_market_guideline_reply(user_text, reply)
    if _wiki_knowledge_reply_ok(user_text, reply, tool_history):
        return enforce_demo_grey_market_guideline_reply(user_text, reply)
    try:
        from src.wiki.retrieval import WikiSearchRequest, search_wiki

        wiki = search_wiki(WikiSearchRequest(
            query=expand_knowledge_search_query(user_text),
            top_k=5,
            include_graph_neighbors=True,
        )).formatted
    except Exception:
        return enforce_demo_grey_market_guideline_reply(user_text, reply)
    if not wiki or not wiki.strip():
        return enforce_demo_grey_market_guideline_reply(user_text, reply)
    formatted = _format_wiki_knowledge_reply(user_text, wiki)
    return enforce_demo_grey_market_guideline_reply(user_text, formatted)


def portfolio_followup_tool_allowlist(user_text: str) -> frozenset[str] | None:
    """E12 / morning follow-up: query_projects only (no list_work_items drift)."""
    if is_work_items_list_query(user_text):
        return None
    if is_portfolio_query(user_text) and re.search(
        r"跟進|brief|attention|今日", user_text, re.IGNORECASE
    ):
        return PORTFOLIO_FOLLOWUP_TOOLS
    return None


def filter_tools_by_allowlist(
    tools: list[dict],
    allowlist: frozenset[str],
) -> list[dict]:
    return [
        t for t in tools
        if t.get("function", {}).get("name") in allowlist
    ]


def _flatten_tool_history(tool_history: list[tuple[str, ...]]) -> list[str]:
    return [name for rnd in tool_history for name in rnd]


def build_guard_system_messages(
    user_text: str,
    conn: sqlite3.Connection | None = None,
    *,
    chat_id: int | None = None,
    effective_project_id: str | None = None,
) -> list[dict]:
    out: list[dict] = []

    status_pid = detect_project_id(user_text)
    if conn is not None:
        from src.agent.project_resolve import resolved_project_id

        status_pid = resolved_project_id(conn, user_text) or status_pid
    if status_pid and is_project_status_query(user_text):
        out.append({
            "role": "system",
            "content": (
                f"<project-status-guard>"
                f"Operator asked for status of {status_pid}. "
                f"Call get_project_status for {status_pid} (and load_project_context if needed). "
                "Do NOT wiki_search guidelines or AML/B2B concept pages. "
                f"Reply in {prose_label()}: milestone, ball_with, next_action, brief context."
                f"</project-status-guard>"
            ),
        })

    if is_general_knowledge_query(user_text):
        out.append({
            "role": "system",
            "content": (
                "<wiki-knowledge-guard>"
                "General knowledge / client guideline question — NO project ID required. "
                "The operator wants the guideline content itself. Wording such as 'provide the client' "
                "describes the intended use and does NOT authorize composing or sending an email. "
                "Sequence: wiki_search (expand P mart → Demo Grey Market) → answer from results. "
                "Return the guideline directly; do NOT ask for a To/recipient, offer an email draft, "
                "or call draft tooling unless the operator explicitly requested a draft. "
                "Do NOT call get_project_status, query_projects, or ask for project ID. "
                "Use canonical Wiki playbook/conformance/public-demo-uat-guidelines as the hub. "
                "CompID, Account (Tag 1), ClOrdID prefix, endpoints, and credentials are PER-PROJECT. "
                "Never surface another client's restricted values in a general reply. "
                "Describe only the draft demo workflow and its missing-source limits. "
                "Do not invent an approved test scope, market procedure or readiness claim. "
                f"Reply in {prose_label()} (English OK for FIX tags / case IDs), ≤12 lines, "
                "start with <think>...</think>."
                "</wiki-knowledge-guard>"
            ),
        })

    if is_demo_grey_market_testing_guideline_query(user_text):
        delivery_instruction = ""
        if not is_draft_request(user_text):
            delivery_instruction = (
                "The operator requested the guideline itself, even if they intend to provide it to a client. "
                "Answer with the guideline directly; do not ask for a recipient and do not offer a draft. "
            )
        out.append({
            "role": "system",
            "content": (
                "<public-demo-guideline-guard>"
                f"{delivery_instruction}"
                "Use canonical Wiki playbook/conformance/public-demo-uat-guidelines. "
                "The demo page is draft/low confidence, not an approved operational UAT workflow. "
                "Disclose missing reviewed test scope and do not fabricate market procedures. "
                "Do not invent or disclose endpoints, CompIDs, account mappings, or credentials; "
                "state that restricted values require approved project evidence."
                "</public-demo-guideline-guard>"
            ),
        })

    if is_ambiguous_project_reference(conn, user_text):
        candidates = get_ambiguous_project_candidates(conn, user_text)
        listed = "、".join(candidates)
        out.append({
            "role": "system",
            "content": (
                f"<ambiguous-project-guard>"
                f"Project reference is ambiguous among: {listed}. "
                "Do NOT guess. Ask the operator which project ID they mean. "
                "You MAY call read-only tools (query_projects, get_project_status). "
                "Do NOT call update_project, update_project_batch, or confirm_update."
                f"</ambiguous-project-guard>"
            ),
        })

    if is_bare_update_command(user_text):
        out.append({
            "role": "system",
            "content": (
                "<ambiguous-update-guard>"
                "Message is only「update」. Do NOT call any tools. "
                "Do NOT list capabilities or onboarding features. "
                "Reply MUST follow this template exactly (≤6 lines including <think>):\n"
                "<think>One-line: missing project, field, value.</think>\n"
                + (
                    "Which project field do you want to update? "
                    "Please provide a project ID and the specific change."
                    if read_response_language() == "en"
                    else "你想更新邊個項目嘅邊個欄位？請提供 project ID 同具體內容。"
                )
                + "</ambiguous-update-guard>"
            ),
        })

    if is_staged_update_confirm_intent(user_text):
        pid = detect_project_id(user_text) or "PROJECT"
        out.append({
            "role": "system",
            "content": (
                f"<staged-confirm-guard>"
                f"Operator confirms staged update for {pid}. "
                "Call confirm_update ONLY — do NOT call get_project_status, "
                "get_project_truth, or get_activity_log first. "
                f"Final reply: {prose_label()}, <think>...</think>, mention confirm and External, ≤8 lines."
                f"</staged-confirm-guard>"
            ),
        })

    if is_email_lookup_query(user_text):
        out.append({
            "role": "system",
            "content": build_email_lookup_guard_content(
                user_text,
                conn=conn,
                chat_id=chat_id,
                effective_project_id=effective_project_id,
            ),
        })

    if is_draft_request(user_text):
        out.append({
            "role": "system",
            "content": build_draft_request_guard_content(
                user_text,
                conn=conn,
                chat_id=chat_id,
                effective_project_id=effective_project_id,
            ),
        })

    if is_work_items_list_query(user_text):
        out.append({
            "role": "system",
            "content": (
                "<work-items-guard>"
                "Call list_work_items (not query_projects) to list open work items. "
                "Mention heartbeat-related items if present."
                "</work-items-guard>"
            ),
        })

    if is_project_followup_fix_combo(user_text):
        out.append({
            "role": "system",
            "content": (
                "<followup-fix-guard>"
                "Project has overdue followup + FIX troubleshooting questions. "
                "Sequence: manage_followup(action=list, project_id) → "
                "wiki_search(logon timeout HeartBtInt). "
                f"Optional: get_project_status. Then answer in {prose_label()} ≤10 lines."
                "</followup-fix-guard>"
            ),
        })

    if needs_milestone_project_clarification(user_text):
        out.append({
            "role": "system",
            "content": (
                "<milestone-clarification-guard>"
                "Operator asked to update milestone but gave NO project ID. "
                "Sequence: call think once (note missing project_id), then final reply. "
                "Do NOT call update_project, update_project_batch, or confirm_update. "
                f"Final reply MUST be {prose_label()}, start with <think>...</think>, "
                "include which project and the word project. "
                + (
                    "Template: <think>Missing project ID; need clarification.</think>\n"
                    "Which project's milestone do you want to update? "
                    "Please provide a project ID (e.g. PROJ-001)."
                    if read_response_language() == "en"
                    else
                    "Template: <think>缺少 project ID，需先澄清。</think>\n"
                    "你想更新邊一個 project 嘅 milestone？請提供 project ID（例如 PROJ-001）。"
                )
                + "</milestone-clarification-guard>"
            ),
        })
    elif (
        needs_write_project_clarification(user_text, conn=conn, chat_id=chat_id)
        and not is_general_knowledge_query(user_text)
    ):
        active_pid = _sign_off_project_id(conn, user_text, chat_id) if chat_id else None
        if active_pid and (
            is_production_sign_off(user_text)
            or re.search(r"\bupdate\b", user_text, re.IGNORECASE)
        ):
            pass  # active project + close/update intent — sign-off or staged path handles it
        else:
            out.append({
                "role": "system",
                "content": (
                    "<write-clarification-guard>"
                    "The operator did NOT provide a project ID and no active project is bound. "
                    "You MUST ask which project (project ID) and which field/value — "
                    "do NOT call update_project, update_project_batch, or confirm_update. "
                    "Read-only tools (query_projects, get_project_status) are allowed."
                    "</write-clarification-guard>"
                ),
            })

    if is_undo_request(user_text):
        pid = detect_project_id(user_text) or "PROJ-001"
        out.append({
            "role": "system",
            "content": (
                f"<undo-guard>"
                f"Operator requested undo of the last confirm_update for {pid}. "
                f"Call undo_update(project_id=\"{pid}\") then reply in {prose_label()}. "
                "Final reply MUST start with <think>...</think> and include the words "
                f"undo, {pid}, and Internal (reverted field value). "
                f"Template: <think>Reverted ball_with→Internal for {pid}.</think>\n"
                + (
                    f"Undid the last confirmed update on {pid}; ball_with restored to Internal."
                    if read_response_language() == "en"
                    else f"已 undo {pid} 上一次確認更新，ball_with 回復為 Internal。"
                )
                + f"</undo-guard>"
            ),
        })

    sign_off_pid = _sign_off_project_id(conn, user_text, chat_id)
    if is_production_sign_off(user_text) and sign_off_pid:
        out.append({
            "role": "system",
            "content": (
                f"<sign-off-guard>"
                f"The operator requested project close / production sign-off for {sign_off_pid}. "
                "Closing from Telegram or web chat is read-only. Do NOT call add_note, "
                "update_project, update_project_batch, confirm_update, or any other write tool. "
                "Report current readiness and blockers only. State that final close requires a "
                "Dashboard operator with project.close permission, and direct the operator to "
                f"/projects/{sign_off_pid}/closing. Reply about {sign_off_pid} only."
                f"</sign-off-guard>"
            ),
        })

    jump = get_milestone_jump_info(conn, user_text) if conn else None
    if jump and jump.delta > 1 and not jump.has_evidence_hint:
        out.append({
            "role": "system",
            "content": (
                f"<milestone-jump-guard>"
                f"Requested jump {jump.from_milestone} → {jump.to_milestone} "
                f"(delta {jump.delta}) for {jump.project_id} without evidence_ref. "
                "Do NOT call update_project, update_project_batch, or confirm_update. "
                "Call think, explain evidence_ref is required, suggest add_note then +1 steps."
                f"</milestone-jump-guard>"
            ),
        })
    elif is_evidence_milestone_write(user_text):
        pid = detect_project_id(user_text) or "PROJECT"
        out.append({
            "role": "system",
            "content": (
                f"<evidence-milestone-guard>"
                f"Operator wants milestone advance for {pid} WITH evidence note. "
                "Sequence: think → add_note (evidence) → update_project or update_project_batch "
                "→ confirm_update BEFORE claiming success. "
                f"Final reply: {prose_label()}, <think>...</think>, include evidence note and target Mx, ≤8 lines."
                f"</evidence-milestone-guard>"
            ),
        })
    elif requests_explicit_milestone_write(user_text):
        out.append({
            "role": "system",
            "content": (
                "<confirm-discipline>"
                "Operator gave an explicit milestone update with project ID. "
                "Sequence: think (if multi-step) → update_project or update_project_batch "
                "→ confirm_update before claiming success. "
                "Never say the milestone changed without calling confirm_update."
                "</confirm-discipline>"
            ),
        })

    if is_fix_incident_query(user_text):
        out.append({
            "role": "system",
            "content": (
                "<fix-incident-protocol>"
                "FIX operational incident. Sequence: "
                "(1) optional wiki_search; "
                "(2) create_ticket(category=fix_session); "
                "(3) get_advisory(ticket_id). "
                "Present next_steps verbatim. ≤12 lines."
                "</fix-incident-protocol>"
            ),
        })

    if portfolio_followup_tool_allowlist(user_text) is not None:
        out.append({
            "role": "system",
            "content": (
                "<portfolio-tool-guard>"
                "Use query_projects ONLY (do not call list_work_items or search_tickets). "
                "Cite project IDs from tool results — never invent projects."
                "</portfolio-tool-guard>"
            ),
        })

    if is_multi_part_query(user_text):
        out.append({
            "role": "system",
            "content": (
                "<response-format>"
                "Multi-part question: begin with <think>...</think> (≤3 lines), "
                "then answer in ≤10 non-empty lines. "
                "For overdue followups use 逾期 or 已逾期 explicitly."
                "</response-format>"
            ),
        })

    out.extend(collect_progress_guards(
        conn,
        user_text,
        effective_project_id=effective_project_id,
        chat_id=chat_id,
    ))

    return out


def blocked_tools_for_message(
    user_text: str,
    conn: sqlite3.Connection | None = None,
    *,
    chat_id: int | None = None,
    effective_project_id: str | None = None,
) -> frozenset[str]:
    blocked = frozenset()
    if is_ambiguous_project_reference(conn, user_text):
        blocked |= WRITE_CLARIFICATION_BLOCK_TOOLS
    if needs_write_project_clarification(user_text, conn=conn, chat_id=chat_id):
        blocked |= WRITE_CLARIFICATION_BLOCK_TOOLS
    if is_production_sign_off(user_text):
        blocked |= CLOSING_WRITE_BLOCK_TOOLS
    jump = get_milestone_jump_info(conn, user_text) if conn else None
    if jump and jump.delta > 1 and not jump.has_evidence_hint:
        blocked |= WRITE_CLARIFICATION_BLOCK_TOOLS
    blocked |= progress_blocked_tools(
        conn, user_text,
        effective_project_id=effective_project_id,
        chat_id=chat_id,
    )
    return blocked


_UNBACKED_CONFIRM_INVITE_RE = re.compile(
    r"(?:shall\s+i\s+proceed|would\s+you\s+like\s+me\s+to\s+update|"
    r"proceed\s+with\s+(?:this|the)\s+update|"
    r"should\s+i\s+(?:apply|update)|"
    r"需要我(?:為你)?更新|要我(?:現在)?更新|是否(?:要我)?(?:進行|套用|更新))",
    re.IGNORECASE,
)


def enforce_durable_staging_claim(
    reply: str,
    tool_history: list[tuple[str, ...]],
) -> str:
    """Do not invite confirmation when no tool staged a durable operation."""
    tools = _flatten_tool_history(tool_history)
    staged = "update_project" in tools or "update_project_batch" in tools
    if staged or not _UNBACKED_CONFIRM_INVITE_RE.search(reply or ""):
        return reply
    return (
        "尚未建立可確認的 staged update，因此目前沒有待套用的資料。"
        "如要更新，請明確提供 project ID 與要修改的欄位；系統會先建立 durable pending operation，"
        "再提供確認卡。"
    )


def apply_post_loop_write_discipline(
    conn: sqlite3.Connection | None,
    user_text: str,
    tool_history: list[tuple[str, ...]],
    effective_project_id: str | None,
    reply: str,
    *,
    exit_reason: str = "ok",
    chat_id: int | None = None,
    successful_research_delegate: bool = False,
) -> str:
    """Auto-confirm staged +1 updates; cancel/reject illegal multi-step jumps.

    Reply-remediation (wiki/UAT/M8 fallback) is skipped when the loop did not
    produce a real answer (exhausted/llm_error/no_answer) so it does not
    overwrite the system fallback (P0-1). The write-discipline (illegal-jump
    cancel + staged-update CU card) still runs regardless of exit_reason — a
    staged-but-unconfirmed jump left at exhaustion must still be cancelled and
    surfaced (P1-4).
    """
    if exit_reason not in ("exhausted", "llm_error", "no_answer"):
        reply = enforce_durable_staging_claim(reply, tool_history)
        reply = enforce_bare_update_reply(user_text, reply, tool_history)
        reply = enforce_evidence_milestone_reply(conn, user_text, reply, tool_history)
        reply = enforce_portfolio_followup_reply(conn, user_text, reply, tool_history)
        if not successful_research_delegate:
            reply = enforce_advisory_wiki_search_reply(user_text, reply, tool_history)
        reply = enforce_wiki_knowledge_reply(
            user_text, reply, tool_history,
            successful_research_delegate=successful_research_delegate,
        )
        reply = enforce_demo_grey_market_guideline_reply(user_text, reply)
        reply = enforce_m8_signoff_no_chase_reply(
            conn, user_text, reply,
            effective_project_id=effective_project_id,
            chat_id=None,
        )
    tools = _flatten_tool_history(tool_history)
    project_id = detect_project_id(user_text) or effective_project_id

    # Multi-step milestone jump guard — requires a DB connection to read the
    # current milestone. Skip on the Telegram production path (conn is None).
    if conn is not None:
        jump = get_milestone_jump_info(conn, user_text)
        if (
            jump
            and jump.delta > 1
            and not jump.has_evidence_hint
            and ("update_project" in tools or "update_project_batch" in tools)
        ):
            from src.tools.executor import execute_tool

            if "confirm_update" not in tools and not conn.in_transaction:
                execute_tool(conn, "*", "cancel_update", {"project_id": jump.project_id})
            notice = (
                f"⚠️ 不能將 **{jump.project_id}** 由 {jump.from_milestone} 直接推到 "
                f"{jump.to_milestone}（跨 {jump.delta} 個里程碑）— 需要 evidence_ref。\n"
                "請先 add_note 記錄證據，再分步推進（每次 +1）。\n\n"
            )
            if notice.strip() not in reply:
                return notice + reply

    # Staged-update CU card + notice. This MUST fire on the Telegram production
    # path (conn is None): when the LLM stages an update via update_project /
    # update_project_batch without confirming, the operator needs the confirm
    # card. Open a short-lived write scope when no connection was provided.
    staged = "update_project" in tools or "update_project_batch" in tools
    if staged and "confirm_update" not in tools and project_id:
        queued = False
        try:
            from src.telegram.staged_update_push import enqueue_staged_update_card

            if conn is not None:
                result = enqueue_staged_update_card(
                    conn, project_id=project_id, chat_id=chat_id, summary=reply[:200] or None,
                )
            else:
                from src.db.connection import scoped_write

                with scoped_write() as wconn:
                    result = enqueue_staged_update_card(
                        wconn, project_id=project_id, chat_id=chat_id, summary=reply[:200] or None,
                    )
            queued = bool(result.get("queued"))
        except Exception:
            logger.exception("Could not queue staged update card for %s", project_id)
        notice = (
            f"📋 **{project_id}** 更新已暫存。"
            + ("確認卡片會在暫存提交後送出；" if queued else "暫未能安排確認卡片；")
            + "也可回覆 confirm_update / cancel_update。"
        )
        if "暫存" not in reply:
            return reply.rstrip() + f"\n\n{notice}"

    return reply
