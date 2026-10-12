"""Shared post-loop processing for all agent surfaces."""

from __future__ import annotations

import json
import logging
import re
import sqlite3
from contextlib import contextmanager, nullcontext

from src.agent.claims import extract_write_claims
from src.agent.grounded_finalizer import finalize_grounded_reply
from src.agent.guards import (
    apply_post_loop_write_discipline,
    is_draft_request,
    is_general_knowledge_query,
)
from src.agent.loop_config import LoopResult
from src.agent.safety import looks_like_fabricated_draft
from src.models.context_frame import ContextFrame

logger = logging.getLogger(__name__)


def _in_inbound_analysis() -> bool:
    # Keep the shared agent module cold-importable without loading email policy.
    from src.email.analysis_execution import active_analysis_policy

    return active_analysis_policy() is not None


@contextmanager
def _post_loop_conn(conn: sqlite3.Connection | None):
    """Telegram agent_loop often passes conn=None; acquire a write conn for rescue."""
    if conn is not None:
        yield conn
        return
    from src.db.connection import scoped_write

    with scoped_write() as wconn:
        yield wconn


def _record_post_loop_tool_result(
    loop_result: LoopResult,
    *,
    tool_name: str,
    result: dict,
    project_id: str | None,
    committed: bool = False,
) -> None:
    """Attach deterministic rescue writes to the authoritative loop ledger."""
    from src.agent.tool_receipts import receipt_from_result
    from src.observability.contracts import new_identifier

    normalized = dict(result)
    if committed:
        normalized["committed"] = True
    receipt = receipt_from_result(
        tool_call_id=new_identifier("posttool"),
        tool_name=tool_name,
        result=normalized,
        project_id=project_id,
    )
    loop_result.tool_history.append((tool_name,))
    loop_result.execution_receipts.append(receipt)


_CORRECTABLE_FIELDS = frozenset({
    "current_milestone", "ball_with", "project_status",
})


def _maybe_log_correction(
    conn: sqlite3.Connection,
    project_id: str,
    field: str,
    new_value: str,
    context_project_truth: str,
) -> None:
    if field not in _CORRECTABLE_FIELDS:
        return
    import re
    m = re.search(rf"\|\s*{re.escape(field)}\s*\|\s*([^|]+)\|", context_project_truth or "")
    prior = m.group(1).rstrip("|").strip() if m else None
    if not prior or prior == new_value:
        return
    try:
        from src.repositories.project_corrections_repo import insert_correction
        insert_correction(
            conn,
            project_id=project_id,
            field=field,
            wrong_value=prior,
            correct_value=new_value,
            commit=False,
        )
        logger.info("Correction staged for %s.%s: %r → %r", project_id, field, prior, new_value)
    except Exception:
        logger.exception("Failed to log correction for %s.%s", project_id, field)


def _pending_draft_row(conn: sqlite3.Connection, project_id: str) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT draft_id, subject, to_email FROM drafts "
        "WHERE project_id = ? AND status IN ('created', 'in_review', 'approved') "
        "ORDER BY created_at DESC LIMIT 1",
        (project_id,),
    ).fetchone()


def _format_draft_success_reply(
    project_id: str | None,
    draft_id: str,
    *,
    card_sent: bool,
    to_email: str | None = None,
) -> str:
    if project_id:
        scope_line = f"已為項目 **{project_id}** 建立郵件草稿（`{draft_id}`）。"
    else:
        who = f" → `{to_email}`" if to_email else ""
        scope_line = f"已建立獨立郵件草稿{who}（`{draft_id}`，不綁定項目）。"
    if card_sent:
        return (
            f"{scope_line}\n"
            "Draft Card 已送出 — 請撳「詳細內容」預覽後再發送。"
        )
    return f"{scope_line}\n草稿預覽將於下方顯示。"


def _format_existing_draft_reply(
    project_id: str,
    draft_id: str,
    *,
    chat_id: int | None,
    existing_to_email: str | None,
    requested_to_email: str | None,
) -> str:
    """Surface a pending draft without replaying a legacy Telegram send."""
    location = (
        "使用 /pending 預覽及確認後再發送。"
        if chat_id is not None
        else "請在 Workspace 草稿清單預覽及確認後再發送。"
    )
    recipient = existing_to_email or "未記錄"
    mismatch = (
        f"你要求的收件人是 `{requested_to_email}`，與現有草稿不同；未為該收件人建立草稿。\n"
        if requested_to_email and requested_to_email.lower() != (existing_to_email or "").lower()
        else ""
    )
    return (
        f"項目 **{project_id}** 已有待審郵件草稿（`{draft_id}`），未建立新草稿。\n"
        f"現有草稿收件人：`{recipient}`。\n{mismatch}{location}"
    )


# Explicit To addresses only — used to gate auto draft_reply rescue.
_EMAIL_RE = re.compile(
    r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}",
)


def extract_explicit_to_email(user_text: str) -> str | None:
    """Pull a single explicit recipient from operator text, or None if ambiguous/absent.

    Auto-rescue must never invent a reply-to-client envelope when the operator
    did not name a To address.
    """
    text = (user_text or "").strip()
    if not text:
        return None
    emails = _EMAIL_RE.findall(text)
    if not emails:
        return None

    # Prefer address bound to to / colleague / send-to phrasing.
    patterns = (
        r"(?:^|[\s,;])(?:to|送俾|寄俾|發給|发给)\s*[:：]?\s*"
        r"(?:my\s+colleague\s+)?<?(" + _EMAIL_RE.pattern + r")>?",
        r"colleague\s+<?(" + _EMAIL_RE.pattern + r")>?",
        r"send\s+(?:the\s+.+?\s+)?to\s+(?:my\s+colleague\s+)?"
        r"<?(" + _EMAIL_RE.pattern + r")>?",
        r"email\s+<?(" + _EMAIL_RE.pattern + r")>?",
    )
    for pat in patterns:
        m = re.search(pat, text, re.IGNORECASE | re.DOTALL)
        if m:
            return m.group(1)

    if len(emails) == 1:
        return emails[0]
    # Multiple addresses, no clear To marker — do not guess.
    return None


def _ask_for_to_email(project_id: str | None) -> str:
    if project_id:
        scope = f"（項目 **{project_id}**）"
    else:
        scope = "（獨立郵件，不綁定項目）"
    return (
        f"需要先確認收件人才能建立草稿{scope}。\n"
        "請回覆明確 **To** 電郵，例如：`draft to name@example.com …`\n"
        "系統不會在沒有指定收件人時自動回覆客戶 thread。"
    )


def rescue_fabricated_draft(
    conn: sqlite3.Connection | None,
    reply: str,
    loop_result: LoopResult,
    effective_project_id: str | None,
    *,
    chat_id: int | None = None,
    user_text: str = "",
) -> str:
    """Create or surface a draft when the model claimed one without calling draft_reply.

    Auto-create via draft_reply is gated: only when the operator text names an
    explicit to_email or one unambiguous allowlisted internal team. Supports
    project-bound and ad-hoc (no project_id) paths. Never invent a client-thread
    reply under sticky project scope without a trusted To.
    """
    # Inbound analysis may stage only source-bound follow-up intents. Draft
    # rescue is an operator-chat fallback, not part of the email command.
    if _in_inbound_analysis():
        return reply

    explicit_request = is_draft_request(user_text)
    if is_general_knowledge_query(user_text) and not explicit_request:
        # Knowledge answers take precedence over unsolicited compose offers.
        # "Provide the client" describes intended use; it does not authorize a
        # draft write or a recipient clarification that replaces the answer.
        return reply

    all_tools_used = {t for rnd in loop_result.tool_history for t in rnd}
    if "draft_reply" in all_tools_used:
        return reply

    draft_claimed = any(c.kind == "draft" for c in extract_write_claims(reply))
    inline_fabrication = looks_like_fabricated_draft(reply)
    if not (explicit_request or draft_claimed or inline_fabrication):
        return reply

    to_email = extract_explicit_to_email(user_text)
    if not to_email:
        from src.email.instruction_fidelity import resolve_trusted_internal_recipient

        to_email = resolve_trusted_internal_recipient(user_text)
    # Project path needs project_id; ad-hoc path needs to_email only.
    if not effective_project_id and not to_email:
        if explicit_request or draft_claimed or inline_fabrication:
            return _ask_for_to_email(None)
        return reply

    if effective_project_id:
        if conn is None:
            from src.db.connection import scoped_read

            with scoped_read() as read_conn:
                pending = _pending_draft_row(read_conn, effective_project_id)
        else:
            pending = _pending_draft_row(conn, effective_project_id)
        if pending:
            # Historical drafts have no durable card intent. Re-sending on
            # each request could duplicate an ambiguous Telegram delivery.
            _record_post_loop_tool_result(
                loop_result,
                tool_name="draft_reply",
                result={
                    "status": "verified_noop",
                    "outcome": "verified_noop",
                    "draft_id": pending["draft_id"],
                },
                project_id=effective_project_id,
            )
            return _format_existing_draft_reply(
                effective_project_id, pending["draft_id"], chat_id=chat_id,
                existing_to_email=pending["to_email"], requested_to_email=to_email,
            )

    if not to_email:
        logger.info(
            "Skipping draft_reply rescue for %s — no explicit or trusted to_email in operator text",
            effective_project_id,
        )
        if explicit_request or draft_claimed or inline_fabrication:
            return _ask_for_to_email(effective_project_id)
        return reply

    with _post_loop_conn(conn) as active_conn:
        logger.warning(
            "Unfulfilled draft for %s — auto-invoking draft_reply rescue to=%s",
            effective_project_id or "adhoc",
            to_email,
        )
        from src.tools.executor import execute_tool

        context = (user_text or reply)[:500]
        rescue_args: dict = {
            "context": context,
            "to_email": to_email,
        }
        if effective_project_id:
            rescue_args["project_id"] = effective_project_id
        if chat_id is not None:
            rescue_args["chat_id"] = chat_id
        rescue_result = execute_tool(
            active_conn,
            "*",
            "draft_reply",
            rescue_args,
            execution_context={"operator_request_text": user_text},
        )
        _record_post_loop_tool_result(
            loop_result,
            tool_name="draft_reply",
            result=rescue_result,
            project_id=effective_project_id,
            committed=bool(rescue_result.get("draft_id")),
        )
        if rescue_result.get("draft_id"):
            return _format_draft_success_reply(
                effective_project_id,
                rescue_result["draft_id"],
                card_sent=bool(rescue_result.get("card_sent")),
                to_email=to_email,
            )
        if explicit_request and rescue_result.get("error"):
            scope = (
                f"項目 **{effective_project_id}**"
                if effective_project_id
                else f"收件人 **{to_email}**"
            )
            return (
                f"無法為{scope}建立郵件草稿："
                f"{rescue_result['error']}\n請稍後重試。"
            )
    return reply


def log_corrections_if_needed(
    conn: sqlite3.Connection | None,
    loop_result: LoopResult,
    context_frame: ContextFrame | None,
) -> None:
    # Model proposals (including denied tool calls) are not email A receipts.
    if _in_inbound_analysis():
        return
    if not context_frame or not context_frame.project_truth:
        return
    if conn is None:
        # Most turns have no project-write tool calls. Do not lease the shared
        # writer merely to scan their already-returned model messages.
        if not any(
            tc.get("function", {}).get("name") in {"update_project", "update_project_batch"}
            for msg in loop_result.messages
            if msg.get("role") == "assistant"
            for tc in (msg.get("tool_calls") or [])
        ):
            return
        with _post_loop_conn(None) as wconn:
            log_corrections_if_needed(wconn, loop_result, context_frame)
        return
    for msg in loop_result.messages:
        if msg.get("role") != "assistant" or not msg.get("tool_calls"):
            continue
        for tc in msg["tool_calls"]:
            fn = tc.get("function", {})
            tool_name = fn.get("name", "")
            if tool_name not in ("update_project", "update_project_batch"):
                continue
            try:
                raw = fn.get("arguments") or "{}"
                args = json.loads(raw) if isinstance(raw, str) else raw
                pid = args.get("project_id", "")
                if tool_name == "update_project":
                    _maybe_log_correction(
                        conn,
                        project_id=pid,
                        field=args.get("field", ""),
                        new_value=str(args.get("new_value", "")),
                        context_project_truth=context_frame.project_truth,
                    )
                else:
                    for change in (args.get("changes") or []):
                        _maybe_log_correction(
                            conn,
                            project_id=pid,
                            field=change.get("field", ""),
                            new_value=str(change.get("new_value", "")),
                            context_project_truth=context_frame.project_truth,
                        )
            except Exception:
                logger.debug("L3 correction check skipped", exc_info=True)


_INCOMPLETE_STATUS_REPLY_RE = re.compile(
    r"^(?:以下是|以下係|Here is).{0,120}(?:進度|status|詳細).*[：:]\s*$",
    re.IGNORECASE,
)


def rescue_incomplete_status_reply(
    conn: sqlite3.Connection | None,
    user_text: str,
    reply: str,
    *,
    effective_project_id: str | None,
    chat_id: int | None,
) -> str:
    """Replace dangling status headers with deterministic /status formatting."""
    from src.agent.progress_advisory import should_inject_progress_advisory

    if not reply or not should_inject_progress_advisory(user_text):
        return reply
    stripped = reply.strip()
    if not _INCOMPLETE_STATUS_REPLY_RE.match(stripped):
        return reply

    from src.agent.active_project import get_active_project_id
    from src.status.answer import answer_status, format_status_telegram

    project_id = effective_project_id
    if conn is None:
        from src.db.connection import scoped_read

        read_scope = scoped_read()
    else:
        read_scope = nullcontext(conn)
    with read_scope as active_conn:
        if not project_id and chat_id is not None:
            project_id = get_active_project_id(active_conn, chat_id)
        if not project_id:
            return reply

        answer = answer_status(active_conn, project_id)
        if not answer:
            return reply

        formatted = format_status_telegram(answer)
        if stripped:
            return f"{stripped}\n\n{formatted}"
        return formatted



def has_successful_research_delegate(messages: list[dict]) -> bool:
    """Detect a completed research delegate from digested tool messages."""
    import json

    tool_names_by_id: dict[str, str] = {}
    for message in messages or []:
        if message.get("role") == "assistant":
            for call in message.get("tool_calls") or []:
                tool_names_by_id[call.get("id", "")] = call.get("function", {}).get("name", "")
            continue
        if message.get("role") != "tool":
            continue
        if tool_names_by_id.get(message.get("tool_call_id", "")) != "delegate":
            continue
        try:
            payload = json.loads(message.get("content") or "{}")
        except (TypeError, ValueError):
            continue
        structured = payload.get("structured") or {}
        if (
            payload.get("tool_name") == "delegate"
            and payload.get("ok") is True
            and structured.get("specialist_type") == "research"
            and structured.get("completion_status") not in {"failed", "exhausted"}
            and not structured.get("error")
        ):
            return True
    return False

def apply_shared_post_loop(
    conn: sqlite3.Connection | None,
    user_text: str,
    loop_result: LoopResult,
    *,
    effective_project_id: str | None,
    context_frame: ContextFrame | None,
    chat_id: int | None = None,
) -> str:
    reply = loop_result.reply
    reply = apply_post_loop_write_discipline(
        conn, user_text, loop_result.tool_history, effective_project_id, reply,
        exit_reason=loop_result.exit_reason,
        chat_id=chat_id,
        successful_research_delegate=has_successful_research_delegate(loop_result.messages),
    )
    reply = rescue_fabricated_draft(
        conn,
        reply,
        loop_result,
        effective_project_id,
        chat_id=chat_id,
        user_text=user_text,
    )
    log_corrections_if_needed(conn, loop_result, context_frame)
    reply = _maybe_append_rule58_draft(
        conn,
        user_text,
        reply,
        loop_result,
        effective_project_id,
        chat_id=chat_id,
    )
    reply = rescue_incomplete_status_reply(
        conn,
        user_text,
        reply,
        effective_project_id=effective_project_id,
        chat_id=chat_id,
    )
    finalized = finalize_grounded_reply(
        reply,
        loop_result=loop_result,
        conn=conn,
        effective_project_id=effective_project_id,
        surface="chat",
    )
    return finalized.text


def _maybe_append_rule58_draft(
    conn: sqlite3.Connection | None,
    user_text: str,
    reply: str,
    loop_result: LoopResult,
    effective_project_id: str | None,
    *,
    chat_id: int | None,
) -> str:
    # Telegram fallback still runs this shared hook during inbound A analysis.
    # Only the IMAP application receipt may authorize business persistence.
    if _in_inbound_analysis() or chat_id is None or not effective_project_id:
        return reply
    from src.agent.progress_advisory import should_inject_progress_advisory
    from src.agent.rule58_draft import try_create_rule58_draft

    if not should_inject_progress_advisory(user_text):
        return reply
    all_tools = {t for rnd in loop_result.tool_history for t in rnd}
    if "draft_reply" in all_tools:
        return reply

    draft_id = try_create_rule58_draft(
        conn, effective_project_id, chat_id=chat_id,
    )
    if not draft_id:
        return reply
    _record_post_loop_tool_result(
        loop_result,
        tool_name="draft_reply",
        result={
            "status": "created",
            "outcome": "created",
            "draft_id": draft_id,
        },
        project_id=effective_project_id,
        committed=True,
    )
    return (
        f"{reply}\n\n"
        f"已為你建立 follow-up 草稿（`{draft_id}`），請於 Dashboard → Drafts 或 /pending 審閱後再發送。"
    )
