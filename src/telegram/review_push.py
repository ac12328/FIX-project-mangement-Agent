"""Telegram push helpers for review cards and applied-update receipts."""

from __future__ import annotations

import asyncio
import re
import sqlite3
import threading
from datetime import datetime, timezone
from typing import Any

from telegram import Bot

from config.settings import CONFIG
from src.db.connection import run_external_io_with_receipt_scope, scoped_write
from src.email.analysis_notice import EmailAnalysisNotice
from src.telegram.cards import build_card, format_card_text
from src.telegram.i18n import t
from src.telegram.markup import card_to_markup
from src.telegram.runtime import send_persisted_message
from src.utils.notification_budget import should_push_with_budget, consume_notification
from src.utils.async_helpers import has_running_loop
from src.utils.push_guard import URGENCY_NORMAL, URGENCY_URGENT

# Re-exported for backward compatibility
from src.telegram.card_push import (  # noqa: F401
    push_error_card_sync,
    push_operational_draft_card_sync,
)


def push_review_card_sync(
    conn: sqlite3.Connection | None,
    *,
    project_id: str,
    update_id: str,
    summary: str,
    reason: str | None = None,
    risk_level: str | None = None,
    evidence_refs: list[str] | None = None,
    proposed_changes: list[dict[str, Any]] | None = None,
    assessment: dict[str, Any] | None = None,
    policy_version: str | None = None,
    source_ref: str | None = None,
    chat_id: int | None = None,
    sender_email: str | None = None,
    subject: str | None = None,
    client_name: str | None = None,
    confidence: float | None = None,
    suggested_action: str = "",
    expected_bot_fingerprint: str | None = None,
) -> dict[str, Any]:
    """Synchronously send a Telegram review card and persist its linkage."""
    token = CONFIG.telegram.bot_token
    if expected_bot_fingerprint is not None:
        import hashlib

        if hashlib.sha256(str(token or "").encode()).hexdigest() != expected_bot_fingerprint:
            return {"telegram_message_id": None, "sent": False, "error": "bot_identity_changed"}
    fallback_allowed = list(getattr(CONFIG.telegram, "allowed_chat_ids", []) or [])
    resolved_chat_id = chat_id or CONFIG.telegram.chat_id or (fallback_allowed[0] if fallback_allowed else 0)
    if not token or not resolved_chat_id:
        return {
            "telegram_message_id": None,
            "sent": False,
            "error": "telegram_not_configured",
        }
    # Budget gate — review cards are urgent
    if conn is None:
        from src.db.connection import scoped_read

        with scoped_read() as rconn:
            allowed = should_push_with_budget(rconn, URGENCY_URGENT)
    else:
        allowed = should_push_with_budget(conn, URGENCY_URGENT)
    if not allowed:
        return {
            "telegram_message_id": None,
            "sent": False,
            "error": "budget_exhausted",
        }
    card_kwargs = dict(
        sender_email=sender_email,
        subject=subject,
        client_name=client_name,
        confidence=confidence,
        suggested_action=suggested_action,
    )
    def _send(receipt_conn: sqlite3.Connection | None) -> dict[str, Any]:
        if has_running_loop():
            return _run_coro_in_thread(
                mode="review",
                token=token,
                chat_id=resolved_chat_id,
                project_id=project_id,
                update_id=update_id,
                summary=summary,
                reason=reason,
                risk_level=risk_level,
                evidence_refs=display_evidence_refs,
                proposed_changes=proposed_changes,
                assessment=assessment,
                policy_version=policy_version,
                source_ref=source_ref,
                **card_kwargs,
            )
        return asyncio.run(_push_review_card(
            receipt_conn,
            token=token,
            chat_id=resolved_chat_id,
            project_id=project_id,
            update_id=update_id,
            summary=summary,
            reason=reason,
            risk_level=risk_level,
            evidence_refs=evidence_refs if receipt_conn is not None else display_evidence_refs,
            proposed_changes=proposed_changes,
            assessment=assessment,
            policy_version=policy_version,
            source_ref=source_ref,
            **card_kwargs,
        ))

    # Resolve evidence summaries before releasing the writer. Network I/O and
    # delivery-receipt scopes must not inherit its serializer lease.
    if evidence_refs and conn is None:
        from src.db.connection import scoped_read

        with scoped_read() as rconn:
            display_evidence_refs = _format_evidence_refs(rconn, evidence_refs)
    else:
        display_evidence_refs = _format_evidence_refs(conn, evidence_refs) if evidence_refs else None
    result = run_external_io_with_receipt_scope(conn, _send) if conn is not None else _send(None)
    if result.get("sent"):
        if conn is None:
            with scoped_write() as wconn:
                consume_notification(wconn, commit=False)
        else:
            consume_notification(conn)
    return result


def push_auto_applied_undo_card_sync(
    conn: sqlite3.Connection | None,
    *,
    project_id: str,
    update_id: str,
    summary: str,
    reason: str | None = None,
    risk_level: str | None = None,
    evidence_refs: list[str] | None = None,
    proposed_changes: list[dict[str, Any]] | None = None,
    applied_fields: list[str] | None = None,
    chat_id: int | None = None,
    sender_email: str | None = None,
    subject: str | None = None,
    client_name: str | None = None,
    confidence: float | None = None,
    suggested_action: str = "",
    expected_bot_fingerprint: str | None = None,
) -> dict[str, Any]:
    """Send auto-applied email update card with undo + dismiss (D1 two options)."""
    token = CONFIG.telegram.bot_token
    if expected_bot_fingerprint is not None:
        import hashlib
        if hashlib.sha256(str(token or "").encode()).hexdigest() != expected_bot_fingerprint:
            return {"telegram_message_id": None, "sent": False, "error": "bot_identity_changed"}
    fallback_allowed = list(getattr(CONFIG.telegram, "allowed_chat_ids", []) or [])
    resolved_chat_id = chat_id or CONFIG.telegram.chat_id or (fallback_allowed[0] if fallback_allowed else 0)
    if not token or not resolved_chat_id:
        return {"telegram_message_id": None, "sent": False, "error": "telegram_not_configured"}
    if expected_bot_fingerprint is not None:
        configured_chat = CONFIG.telegram.chat_id or (
            fallback_allowed[0] if fallback_allowed else 0
        )
        if int(configured_chat or 0) != int(resolved_chat_id):
            return {"telegram_message_id": None, "sent": False, "error": "recipient_changed"}
    if conn is None:
        from src.db.connection import scoped_read
        with scoped_read() as rconn:
            allowed = should_push_with_budget(rconn, URGENCY_URGENT)
    else:
        allowed = should_push_with_budget(conn, URGENCY_URGENT)
    if not allowed:
        return {"telegram_message_id": None, "sent": False, "error": "budget_exhausted"}
    card_kwargs = dict(
        sender_email=sender_email,
        subject=subject,
        client_name=client_name,
        confidence=confidence,
        suggested_action=suggested_action,
    )
    def _send(receipt_conn: sqlite3.Connection | None) -> dict[str, Any]:
        if has_running_loop():
            return _run_coro_in_thread(
                mode="auto_applied_undo",
                token=token,
                chat_id=resolved_chat_id,
                project_id=project_id,
                update_id=update_id,
                summary=summary,
                reason=reason,
                risk_level=risk_level,
                evidence_refs=evidence_refs,
                proposed_changes=proposed_changes,
                applied_fields=applied_fields,
                **card_kwargs,
            )
        return asyncio.run(_push_auto_applied_undo_card(
            receipt_conn,
            token=token,
            chat_id=resolved_chat_id,
            project_id=project_id,
            update_id=update_id,
            summary=summary,
            reason=reason,
            risk_level=risk_level,
            evidence_refs=evidence_refs,
            proposed_changes=proposed_changes,
            applied_fields=applied_fields,
            **card_kwargs,
        ))

    result = run_external_io_with_receipt_scope(conn, _send) if conn is not None else _send(None)
    if result.get("sent"):
        if conn is None:
            from src.db.connection import scoped_write
            with scoped_write() as wconn:
                consume_notification(wconn, commit=False)
        else:
            consume_notification(conn)
    return result


def push_email_info_card_sync(
    conn: sqlite3.Connection | None,
    *,
    project_id: str,
    summary: str,
    suggested_action: str = "",
    sender_email: str | None = None,
    subject: str | None = None,
    client_name: str | None = None,
    chat_id: int | None = None,
    notice: EmailAnalysisNotice | None = None,
    expected_bot_fingerprint: str | None = None,
    require_persistence: bool = False,
    source_email_id: str | None = None,
    combined_advisory: bool = False,
    advisory_fingerprint: str | None = None,
    combined_delivery_group: str | None = None,
    advisory_urgent: bool = False,
) -> dict[str, Any]:
    """Analysis-only email card (no truth write) with DISMISS."""
    token = CONFIG.telegram.bot_token
    if expected_bot_fingerprint is not None:
        import hashlib
        if hashlib.sha256(str(token or "").encode()).hexdigest() != expected_bot_fingerprint:
            return {"telegram_message_id": None, "sent": False, "error": "bot_identity_changed"}
    fallback_allowed = list(getattr(CONFIG.telegram, "allowed_chat_ids", []) or [])
    resolved_chat_id = chat_id or CONFIG.telegram.chat_id or (
        fallback_allowed[0] if fallback_allowed else 0
    )
    if not token or not resolved_chat_id:
        return {"telegram_message_id": None, "sent": False, "error": "telegram_not_configured"}
    if expected_bot_fingerprint is not None:
        configured_chat = CONFIG.telegram.chat_id or (
            fallback_allowed[0] if fallback_allowed else 0
        )
        if int(configured_chat or 0) != int(resolved_chat_id):
            return {"telegram_message_id": None, "sent": False, "error": "recipient_changed"}
    if conn is None:
        from src.db.connection import scoped_read

        with scoped_read() as rconn:
            allowed = should_push_with_budget(rconn, URGENCY_URGENT if advisory_urgent else URGENCY_NORMAL)
    else:
        allowed = should_push_with_budget(conn, URGENCY_URGENT if advisory_urgent else URGENCY_NORMAL)
    if not allowed:
        return {"telegram_message_id": None, "sent": False, "error": "budget_exhausted"}

    from src.email.analysis_notice import build_email_analysis_notice, render_email_analysis_notice
    from src.telegram.i18n import get_ui_language
    from src.telegram.email_update_format import build_email_card_title

    locale = get_ui_language()
    notice = notice or build_email_analysis_notice(summary=summary, suggested_action=suggested_action)
    identity = f"{project_id} · {client_name}" if client_name else project_id
    body = "\n".join(filter(None, [identity[:180], (subject or "")[:200], sender_email]))
    body += "\n\n" + render_email_analysis_notice(notice, language=locale)
    title = (f"{'Email analysis and project advice' if locale == 'en' else '郵件分析及專案建議'} · {project_id}"
             if combined_advisory else build_email_card_title("info_only", project_id))

    async def _push() -> dict[str, Any]:
        bot = Bot(token=token)
        # Use plain text intentionally; source excerpts are not markup.
        # A real URL button replaces a raw Markdown link and misleading profile-I hint.
        from telegram import InlineKeyboardButton, InlineKeyboardMarkup
        from urllib.parse import quote
        dismiss = "Dismiss notification" if locale == "en" else "略過此通知"
        rows = [[InlineKeyboardButton(dismiss, callback_data=f"I:DISMISS:{project_id}")]]
        if combined_advisory:
            from src.telegram.i18n import button_display
            rows.insert(0, [InlineKeyboardButton(button_display(action.replace("_", " ")), callback_data=f"O:{action}:{project_id}")
                            for action in ("DRAFT_EMAIL", "I_HANDLE", "ASK_MORE")])
        base = getattr(CONFIG, "dashboard_base_url", "")
        if base:
            label = "View project" if locale == "en" else "查看專案"
            rows.insert(0, [InlineKeyboardButton(label, url=f"{base.rstrip('/')}/projects/{quote(project_id, safe='')}")])
        message_ids = await send_persisted_message(
            bot,
            conn,
            chat_id=resolved_chat_id,
            text=f"{title}\n━━━━━━━━━━━━━━━━━━━━━━━\n{body}",
            format_mode="plain",
            project_id=project_id,
            message_kind="card",
            reply_markup=InlineKeyboardMarkup(rows),
            metadata={"card_profile": "I", "combined_project_advisory": combined_advisory, "email_info_only": True,
                      "analysis_notice": notice.model_dump(), "email_id": source_email_id,
                      "bot_fingerprint": expected_bot_fingerprint,
                      "advisory_fingerprint": advisory_fingerprint,
                      "combined_delivery_group": combined_delivery_group},
            require_persistence=require_persistence,
        )
        mid = message_ids[-1] if message_ids else None
        return {
            "telegram_message_id": mid,
            "sent": mid is not None,
            "sent_at": datetime.now(timezone.utc).isoformat(),
        }

    if has_running_loop():
        result_box: dict[str, Any] = {}
        err_box: list[BaseException] = []

        def _runner() -> None:
            try:
                result_box["value"] = asyncio.run(_push())
            except BaseException as exc:
                err_box.append(exc)

        th = threading.Thread(target=_runner, daemon=True)
        th.start()
        th.join()
        if err_box:
            raise err_box[0]
        result = result_box["value"]
    else:
        result = asyncio.run(_push())
    if result.get("sent"):
        if conn is None:
            with scoped_write() as wconn:
                consume_notification(wconn, commit=False)
        else:
            consume_notification(conn)
    return result


def push_applied_update_sync(
    conn: sqlite3.Connection,
    *,
    project_id: str,
    update_id: str,
    changes_applied: list[dict[str, Any]] | None = None,
    chat_id: int | None = None,
) -> dict[str, Any]:
    """Synchronously send a lightweight Telegram receipt for an accepted update."""
    token = CONFIG.telegram.bot_token
    fallback_allowed = list(getattr(CONFIG.telegram, "allowed_chat_ids", []) or [])
    resolved_chat_id = chat_id or CONFIG.telegram.chat_id or (fallback_allowed[0] if fallback_allowed else 0)
    if not token or not resolved_chat_id:
        return {
            "telegram_message_id": None,
            "sent": False,
            "error": "telegram_not_configured",
        }
    # Budget gate — applied-update receipts are normal urgency
    if not should_push_with_budget(conn, URGENCY_NORMAL):
        return {
            "telegram_message_id": None,
            "sent": False,
            "error": "budget_exhausted",
        }
    if has_running_loop():
        result = _run_coro_in_thread(
            mode="applied_update",
            token=token,
            chat_id=resolved_chat_id,
            project_id=project_id,
            update_id=update_id,
            changes_applied=changes_applied,
        )
    else:
        coro = _push_applied_update(
            conn,
            token=token,
            chat_id=resolved_chat_id,
            project_id=project_id,
            update_id=update_id,
            changes_applied=changes_applied,
        )
        result = asyncio.run(coro)
    if result.get("sent"):
        consume_notification(conn)
    return result


async def _push_review_card(
    conn: sqlite3.Connection | None,
    *,
    token: str,
    chat_id: int,
    project_id: str,
    update_id: str,
    summary: str,
    reason: str | None = None,
    risk_level: str | None = None,
    evidence_refs: list[str] | None = None,
    proposed_changes: list[dict[str, Any]] | None = None,
    assessment: dict[str, Any] | None = None,
    policy_version: str | None = None,
    source_ref: str | None = None,
    sender_email: str | None = None,
    subject: str | None = None,
    client_name: str | None = None,
    confidence: float | None = None,
    suggested_action: str = "",
) -> dict[str, Any]:
    """Async implementation for Telegram review card delivery."""
    from src.telegram.email_update_format import build_email_card_title

    bot = Bot(token=token)
    display_evidence = _format_evidence_refs(conn, evidence_refs)
    body = _build_review_body(
        summary=summary,
        reason=reason,
        risk_level=risk_level,
        evidence_refs=display_evidence,
        proposed_changes=proposed_changes,
        project_id=project_id,
        write_status="pending_review",
        sender_email=sender_email,
        subject=subject,
        client_name=client_name,
        confidence=confidence,
        suggested_action=suggested_action,
        assessment=assessment,
        policy_version=policy_version,
        source_ref=source_ref,
    )
    title = build_email_card_title("pending_review", project_id)
    card = build_card("A", title, body, project_id=project_id)
    message_ids = await send_persisted_message(
        bot,
        conn,
        chat_id=chat_id,
        text=format_card_text(card),
        project_id=project_id,
        message_kind="card",
        reply_markup=card_to_markup(card),
        object_links=[
            {
                "object_type": "proposed_update",
                "object_ref": update_id,
                "project_id": project_id,
            }
        ],
        metadata={
            "card_profile": "A",
            "review_update_id": update_id,
            "risk_level": risk_level,
        },
    )
    telegram_message_id = message_ids[-1] if message_ids else None
    if telegram_message_id is not None:
        if conn is not None:
            conn.execute(
                "UPDATE proposed_updates SET review_card_id = ? WHERE update_id = ?",
                (str(telegram_message_id), update_id),
            )
            conn.commit()
        else:
            from src.db.connection import scoped_write as _sw
            with _sw() as wconn:
                wconn.execute(
                    "UPDATE proposed_updates SET review_card_id = ? WHERE update_id = ?",
                    (str(telegram_message_id), update_id),
                )
    return {
        "telegram_message_id": telegram_message_id,
        "sent": telegram_message_id is not None,
        "sent_at": datetime.now(timezone.utc).isoformat(),
    }


async def _push_auto_applied_undo_card(
    conn: sqlite3.Connection | None,
    *,
    token: str,
    chat_id: int,
    project_id: str,
    update_id: str,
    summary: str,
    reason: str | None = None,
    risk_level: str | None = None,
    evidence_refs: list[str] | None = None,
    proposed_changes: list[dict[str, Any]] | None = None,
    applied_fields: list[str] | None = None,
    sender_email: str | None = None,
    subject: str | None = None,
    client_name: str | None = None,
    confidence: float | None = None,
    suggested_action: str = "",
) -> dict[str, Any]:
    """Profile L card: applied diff with UNDO + DISMISS (exactly two options)."""
    from src.telegram.email_update_format import build_email_card_title

    bot = Bot(token=token)
    body = _build_review_body(
        summary=summary,
        reason=reason,
        risk_level=risk_level,
        evidence_refs=evidence_refs,
        proposed_changes=proposed_changes,
        project_id=project_id,
        write_status="applied",
        sender_email=sender_email,
        subject=subject,
        client_name=client_name,
        confidence=confidence,
        suggested_action=suggested_action,
    )
    title = build_email_card_title("applied", project_id)
    card = build_card(
        "L",
        title,
        body,
        project_id=project_id,
        extra_buttons=[
            # D1: two options only — undo or accept/dismiss
            [("UNDO", f"L:UNDO:{update_id}"), ("DISMISS", f"L:DISMISS:{project_id}")],
        ],
    )
    message_ids = await send_persisted_message(
        bot,
        conn,
        chat_id=chat_id,
        text=format_card_text(card),
        project_id=project_id,
        message_kind="card",
        reply_markup=card_to_markup(card),
        object_links=[
            {
                "object_type": "proposed_update",
                "object_ref": update_id,
                "project_id": project_id,
            }
        ],
        metadata={
            "card_profile": "L",
            "auto_applied": True,
            "review_update_id": update_id,
            "risk_level": risk_level,
        },
    )
    telegram_message_id = message_ids[-1] if message_ids else None
    if telegram_message_id is not None:
        if conn is not None:
            conn.execute(
                "UPDATE proposed_updates SET review_card_id = ? WHERE update_id = ?",
                (str(telegram_message_id), update_id),
            )
            conn.commit()
        else:
            with scoped_write() as wconn:
                wconn.execute(
                    "UPDATE proposed_updates SET review_card_id = ? WHERE update_id = ?",
                    (str(telegram_message_id), update_id),
                )
    return {
        "telegram_message_id": telegram_message_id,
        "sent": telegram_message_id is not None,
        "sent_at": datetime.now(timezone.utc).isoformat(),
    }


async def _push_applied_update(
    conn: sqlite3.Connection | None,
    *,
    token: str,
    chat_id: int,
    project_id: str,
    update_id: str,
    changes_applied: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Async implementation for Telegram applied-update receipt delivery."""
    bot = Bot(token=token)
    text = _build_applied_update_body(project_id=project_id, changes_applied=changes_applied)
    message_ids = await send_persisted_message(
        bot,
        None,
        chat_id=chat_id,
        text=text,
        project_id=project_id,
        message_kind="receipt",
        object_links=[
            {
                "object_type": "proposed_update",
                "object_ref": update_id,
                "project_id": project_id,
            }
        ],
        metadata={
            "receipt_type": "applied_update",
            "review_update_id": update_id,
            "changes_applied": len(changes_applied or []),
        },
    )
    telegram_message_id = message_ids[-1] if message_ids else None
    return {
        "telegram_message_id": telegram_message_id,
        "sent": telegram_message_id is not None,
        "sent_at": datetime.now(timezone.utc).isoformat(),
    }


def _format_evidence_refs(
    conn: sqlite3.Connection | None,
    evidence_refs: list[str] | None,
) -> list[str] | None:
    """Return compact evidence summaries while preserving the audit reference."""
    if not evidence_refs:
        return None
    if conn is None:
        return evidence_refs

    from src.repositories.evidence_repo import get_evidence_by_id

    formatted: list[str] = []
    for ref in evidence_refs:
        try:
            evidence = get_evidence_by_id(conn, ref)
        except Exception:
            evidence = None
        if evidence is None:
            formatted.append(ref)
            continue
        summary = " ".join((evidence.content_summary or "").split())[:240]
        formatted.append(f"{summary} [{ref}]" if summary else ref)
    return formatted


_MAX_REVIEW_BODY = 3000
_MAX_REVIEW_LINE = 700
_REASONING_BLOCK_RE = re.compile(
    r"<(?:think|analysis|reasoning)\b[^>]*>.*?</(?:think|analysis|reasoning)\s*>",
    re.IGNORECASE | re.DOTALL,
)
_ESCAPED_REASONING_BLOCK_RE = re.compile(
    r"(?:&lt;|\\u003c)(?:think|analysis|reasoning)(?:&gt;|\\u003e).*?"
    r"(?:&lt;/|\\u003c/)(?:think|analysis|reasoning)(?:&gt;|\\u003e)",
    re.IGNORECASE | re.DOTALL,
)
_RAW_JSON_RE = re.compile(r"\{\s*[\"'][^{}]{0,120}[\"']\s*:\s*.*?\}", re.DOTALL)
_TOOL_JSON_RE = re.compile(r"\b(?:tool_calls?|function_call|arguments)\b", re.IGNORECASE)
_REASONING_OPEN_RE = re.compile(
    r"<(?:think|analysis|reasoning)\b[^>]*>|"
    r"(?:&lt;|\\u003c)(?:think|analysis|reasoning)(?:&gt;|\\u003e)",
    re.IGNORECASE,
)


def _safe_display(value: Any, limit: int) -> str:
    """Return bounded plain text, dropping reasoning blocks and JSON/tool payloads."""
    if not isinstance(value, str):
        return ""
    text = _ESCAPED_REASONING_BLOCK_RE.sub("", value)
    text = _REASONING_BLOCK_RE.sub("", text)
    unclosed = _REASONING_OPEN_RE.search(text)
    if unclosed:
        text = text[:unclosed.start()]
    if _TOOL_JSON_RE.search(text) or _RAW_JSON_RE.search(text):
        text = text[: min((pos for pos in (text.find("{"), text.find("[")) if pos >= 0), default=len(text))]
    text = " ".join(text.replace("<think>", "").replace("</think>", "").split())
    if len(text) > limit:
        text = text[: max(0, limit - 3)].rstrip() + "..."
    return text


def _safe_review_line(value: Any, limit: int) -> str:
    """Sanitize one display line while preserving section gaps and indentation."""
    if not isinstance(value, str) or not value:
        return ""
    indent_length = min(len(value) - len(value.lstrip(" ")), 8)
    indent = " " * indent_length
    content = _safe_display(value[indent_length:], max(1, limit - indent_length))
    return f"{indent}{content}" if content else ""


def _safe_review_changes(proposed_changes: Any) -> list[dict[str, str]]:
    if not isinstance(proposed_changes, list):
        return []
    changes: list[dict[str, str]] = []
    for item in proposed_changes[:12]:
        if not isinstance(item, dict):
            continue
        field = _safe_display(item.get("field"), 80) or "?"
        old_value = _safe_display(item.get("old_value"), 220) or "—"
        new_value = _safe_display(item.get("new_value"), 220) or "(empty)"
        changes.append({"field": field, "old_value": old_value, "new_value": new_value})
    return changes


def _build_review_body(
    *, summary: str, reason: str | None = None, risk_level: str | None = None,
    evidence_refs: list[str] | None = None, proposed_changes: list[dict[str, Any]] | None = None,
    project_id: str | None = None, write_status: str = "pending_review",
    sender_email: str | None = None, subject: str | None = None, client_name: str | None = None,
    confidence: float | None = None, suggested_action: str = "", assessment: dict[str, Any] | None = None,
    policy_version: str | None = None, source_ref: str | None = None,
) -> str:
    """Build a bounded, decision-first review card without raw internal metadata."""
    del confidence  # Scalar confidence is informational and does not drive review warnings.
    safe_changes = _safe_review_changes(proposed_changes)
    details = assessment if isinstance(assessment, dict) else {}
    if write_status == "pending_review":
        lines = _build_pending_review_sections(
            summary=summary,
            reason=reason,
            risk_level=risk_level,
            evidence_refs=evidence_refs,
            safe_changes=safe_changes,
            sender_email=sender_email,
            subject=subject,
            client_name=client_name,
            assessment=details,
            policy_version=policy_version,
            source_ref=source_ref,
        )
    else:
        lines = [f"📋 {_safe_display(summary, 500) or 'Review requested.'}"]
        if project_id:
            lines.append(f"Project: {_safe_display(project_id, 80)}")
            for label, value in (("Client", client_name), ("Subject", subject), ("From", sender_email)):
                safe_value = _safe_display(value, 180)
                if safe_value:
                    lines.append(f"{label}: {safe_value}")
        safe_risk = _safe_display(risk_level, 40)
        if safe_risk:
            risk_emoji = {"LOW": "🟢", "MEDIUM": "🟡", "HIGH": "🔴"}.get(safe_risk.upper(), "⚪")
            lines.append(t("push.review_risk").format(emoji=risk_emoji, level=safe_risk))
        for change in safe_changes:
            lines.append(f"• {change['field']}: {change['old_value']} → {change['new_value']}")
        safe_reason = _safe_display(reason, 400)
        if safe_reason:
            lines.append(t("push.review_reason").format(reason=safe_reason))
        safe_action = _safe_display(suggested_action, 240)
        if safe_action:
            lines.append(f"Suggested action: {safe_action}")
        lines.extend(_review_evidence_lines(details, evidence_refs, source_ref))
        criteria = _criteria_summary(details)
        if criteria:
            lines.append(criteria)
        raw_changes = proposed_changes if isinstance(proposed_changes, list) else []
        if _requires_explicit_warning(raw_changes, details):
            lines.append("Warning: protected-field transition requires review.")
        safe_policy = _safe_display(policy_version, 120)
        if safe_policy:
            lines.append(f"Policy: {safe_policy}")

    bounded_lines = [_safe_review_line(line, _MAX_REVIEW_LINE) for line in lines]
    body = "\n".join(bounded_lines)
    body = re.sub(r"\n{3,}", "\n\n", body)
    return body[:_MAX_REVIEW_BODY].rstrip()


def _build_pending_review_sections(
    *,
    summary: str,
    reason: str | None,
    risk_level: str | None,
    evidence_refs: list[str] | None,
    safe_changes: list[dict[str, str]],
    sender_email: str | None,
    subject: str | None,
    client_name: str | None,
    assessment: dict[str, Any],
    policy_version: str | None,
    source_ref: str | None,
) -> list[str]:
    from src.telegram.email_update_format import field_label, truncate_summary
    from src.telegram.i18n import get_ui_language

    locale = get_ui_language()
    is_zh = locale == "zh-TW"
    headings = {
        "changes": "建議變更" if is_zh else "PROPOSED CHANGES",
        "why": "更新依據" if is_zh else "WHY THIS UPDATE",
        "source": "來源" if is_zh else "SOURCE",
        "notes": "審核提示" if is_zh else "REVIEW NOTES",
    }
    labels = {
        "current": "目前" if is_zh else "Current",
        "proposed": "建議" if is_zh else "Proposed",
        "client": "客戶" if is_zh else "Client",
        "sender": "寄件者" if is_zh else "From",
        "subject": "主旨" if is_zh else "Subject",
        "risk": "風險" if is_zh else "Risk",
        "review_reason": "審核原因" if is_zh else "Review reason",
        "remaining_gate": "尚待完成" if is_zh else "Remaining gate",
    }

    lines = ["尚未套用；批准後才會更新以下欄位。" if is_zh else "Not applied yet; approval will update the fields below.", "", headings["changes"]]
    if safe_changes:
        for change in safe_changes:
            label = field_label(change["field"], locale)
            lines.append(f"• {label}")
            _append_review_value(lines, labels["current"], change["old_value"])
            _append_review_value(lines, labels["proposed"], change["new_value"])
    else:
        empty_change = (
            "未提供欄位變更；批准前請先核對。"
            if is_zh else
            "No field changes supplied; review before approval."
        )
        lines.append(f"• {empty_change}")

    lines.extend(["", headings["why"]])
    safe_summary = _safe_display(truncate_summary(summary, 420), 440)
    summary_items = _split_review_items(safe_summary, split_semicolons=False)
    if summary_items:
        lines.extend(f"• {item}" for item in summary_items)

    source_lines = _build_review_source_lines(
        locale=locale,
        client_name=client_name,
        sender_email=sender_email,
        subject=subject,
        assessment=assessment,
        evidence_refs=evidence_refs,
        source_ref=source_ref,
    )
    if source_lines:
        lines.extend(["", headings["source"]])
        lines.extend(source_lines)

    lines.extend(["", headings["notes"]])
    safe_risk = _safe_display(risk_level, 40)
    if safe_risk:
        risk_emoji = {
            "LOW": "🟢",
            "MEDIUM": "🟡",
            "HIGH": "🔴",
        }.get(safe_risk.upper(), "⚪")
        lines.append(f"{risk_emoji} {labels['risk']}: {safe_risk}")
    safe_reason = _safe_display(reason, 300)
    if safe_reason and not _is_source_only_reason(safe_reason, sender_email):
        lines.append(f"• {labels['review_reason']}: {safe_reason}")
    raw_fields = {change["field"] for change in safe_changes}
    if raw_fields & {"current_milestone", "project_status", "ball_with"}:
        lines.append(
            "• 將修改受保護的項目狀態；請核對證據及轉換。"
            if is_zh else
            "• Protected project state will change; verify the evidence and transition."
        )
    criteria = _criteria_summary(assessment)
    if criteria:
        lines.append(f"• {labels['remaining_gate']}: {criteria}")
    safe_policy = _safe_display(policy_version, 100)
    if safe_policy:
        lines.append(f"• Policy: {safe_policy}")
    return lines


def _append_review_value(lines: list[str], label: str, value: str) -> None:
    """Append a labelled value as mobile-friendly action bullets."""
    lines.append(f"  {label}:")
    items = _split_review_items(value, split_semicolons=True)
    if not items:
        items = ["—"]
    lines.extend(f"    • {item}" for item in items)


def _split_review_items(
    value: str,
    *,
    split_semicolons: bool,
    max_items: int = 4,
) -> list[str]:
    """Split dense review prose into a small number of scannable items."""
    text = " ".join((value or "").split())
    if not text:
        return []
    if split_semicolons:
        parts = re.split(r"\s*;\s*", text)
    else:
        parts = re.split(r"(?<=[.!?。！？])\s+", text)
    parts = [part.strip() for part in parts if part.strip()]
    if len(parts) > max_items:
        parts = parts[: max_items - 1] + [" ".join(parts[max_items - 1 :])]
    return parts or [text]


def _build_review_source_lines(
    *,
    locale: str,
    client_name: str | None,
    sender_email: str | None,
    subject: str | None,
    assessment: dict[str, Any],
    evidence_refs: list[str] | None,
    source_ref: str | None,
) -> list[str]:
    """Build a compact source block and omit metadata repeated by evidence text."""
    is_zh = locale == "zh-TW"
    labels = {
        "client": "客戶" if is_zh else "Client",
        "sender": "寄件者" if is_zh else "From",
        "subject": "主旨" if is_zh else "Subject",
    }
    evidence_lines = _review_evidence_lines(
        assessment,
        evidence_refs,
        source_ref,
        locale=locale,
    )
    evidence_blob = " ".join(evidence_lines).casefold()
    lines: list[str] = []
    safe_client = _safe_display(client_name, 120)
    safe_sender = _safe_display(sender_email, 120)
    safe_subject = _safe_display(subject, 150)
    if safe_client:
        lines.append(f"• {labels['client']}: {safe_client}")
    if safe_sender and safe_sender.casefold() not in evidence_blob:
        lines.append(f"• {labels['sender']}: {safe_sender}")
    if safe_subject and safe_subject.casefold() not in evidence_blob:
        lines.append(f"• {labels['subject']}: {safe_subject}")
    lines.extend(f"• {line}" for line in evidence_lines)
    return lines


def _is_source_only_reason(reason: str, sender_email: str | None) -> bool:
    """Suppress review reasons that merely repeat the sender metadata."""
    sender = _safe_display(sender_email, 120)
    if not sender:
        return False
    normalized = " ".join(reason.casefold().replace("：", ":").split()).strip(" .")
    address = sender.casefold()
    return normalized in {
        address,
        f"from {address}",
        f"from: {address}",
        f"來自 {address}",
        f"寄件者: {address}",
    }


def _review_evidence_lines(
    assessment: dict[str, Any],
    evidence_refs: list[str] | None,
    source_ref: str | None,
    *,
    locale: str = "en",
) -> list[str]:
    is_zh = locale == "zh-TW"
    evidence_label = "證據" if is_zh else "Evidence"
    source_label = "來源" if is_zh else "Source"
    strongest = _strongest_citation(assessment)
    if strongest:
        quote, citation_ref = strongest
        lines = [f'{evidence_label}: "{quote}"']
        safe_ref = _human_evidence_ref(citation_ref)
        if safe_ref:
            lines.append(f"{source_label}: {safe_ref}")
        return lines
    refs = [_human_evidence_ref(ref) for ref in evidence_refs[:2]] if isinstance(evidence_refs, list) else []
    refs = [ref for ref in refs if ref]
    if refs:
        return [f"{evidence_label}: {', '.join(refs)}"]
    safe_source = _human_evidence_ref(source_ref)
    return [f"{source_label}: {safe_source}"] if safe_source else []


def _human_evidence_ref(value: Any) -> str | None:
    text = _safe_display(value, 260)
    if not text:
        return None
    text = re.sub(r"\s*\[[0-9a-f]{8}-[0-9a-f-]{27,}\]\s*$", "", text, flags=re.I)
    text = re.sub(r"\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b", "", text, flags=re.I)
    return " ".join(text.split()).strip(" -:[]") or None


def _strongest_citation(assessment: dict[str, Any]) -> tuple[str, str | None] | None:
    for slot in ("milestone_assessment", "status_assessment", "ball_with_assessment", "next_action_assessment"):
        value = assessment.get(slot)
        citations = value.get("citations") if isinstance(value, dict) else None
        if not isinstance(citations, list):
            continue
        for citation in citations:
            if not isinstance(citation, dict):
                continue
            quote = _safe_display(citation.get("quote"), 400)
            citation_ref = _safe_display(citation.get("source_ref"), 260)
            if quote:
                return quote, citation_ref or None
    return None


def _criteria_summary(assessment: dict[str, Any]) -> str | None:
    criteria = assessment.get("milestone_criteria")
    if not isinstance(criteria, list):
        return None
    counts = {key: 0 for key in ("met", "missing", "contradicted")}
    for item in criteria:
        result = item.get("result") if isinstance(item, dict) else None
        if isinstance(result, str) and result in counts:
            counts[result] += 1
    return "Criteria: " + "; ".join(f"{counts[key]} {key}" for key in counts) if any(counts.values()) else None


def _requires_explicit_warning(changes: list[Any], assessment: dict[str, Any]) -> bool:
    protected = {"current_milestone", "project_status", "ball_with"}
    if any(isinstance(change, dict) and change.get("field") in protected for change in changes):
        return True
    warning_decisions = {"needs_review", "contradiction", "clarification_required", "terminal", "jump", "revert"}
    for key in ("milestone_assessment", "status_assessment", "ball_with_assessment", "next_action_assessment"):
        slot = assessment.get(key)
        decision = slot.get("decision") if isinstance(slot, dict) else None
        if isinstance(decision, str) and decision in warning_decisions:
            return True
    return False


def _build_applied_update_body(
    *,
    project_id: str,
    changes_applied: list[dict[str, Any]] | None = None,
) -> str:
    changes = changes_applied or []
    lines = [
        t("push.update_applied"),
        t("push.update_applied_body").format(project_id=project_id, count=len(changes)),
    ]
    if changes:
        lines.append("")
        for change in changes[:3]:
            fld = change.get("field", "?")
            new_value = change.get("new_value")
            preview = str(new_value) if new_value is not None else "null"
            if len(preview) > 350:
                preview = preview[:347] + "..."
            lines.append(f"  {t('push.update_applied_detail').format(field=fld, value=preview)}")
        if len(changes) > 3:
            lines.append(t("push.update_more").format(count=len(changes) - 3))
    lines.append("")
    lines.append(t("push.update_applied_footer"))
    return "\n".join(lines)


def _run_coro_in_thread(
    *,
    mode: str,
    token: str,
    chat_id: int,
    project_id: str,
    update_id: str,
    summary: str | None = None,
    reason: str | None = None,
    risk_level: str | None = None,
    evidence_refs: list[str] | None = None,
    proposed_changes: list[dict[str, Any]] | None = None,
    assessment: dict[str, Any] | None = None,
    policy_version: str | None = None,
    source_ref: str | None = None,
    changes_applied: list[dict[str, Any]] | None = None,
    applied_fields: list[str] | None = None,
    sender_email: str | None = None,
    subject: str | None = None,
    client_name: str | None = None,
    confidence: float | None = None,
    suggested_action: str = "",
):
    result: dict[str, Any] = {}
    error: list[BaseException] = []
    email_kw = dict(
        sender_email=sender_email,
        subject=subject,
        client_name=client_name,
        confidence=confidence,
        suggested_action=suggested_action,
    )

    def _runner() -> None:
        try:
            if mode == "review":
                result["value"] = asyncio.run(
                    _push_review_card(
                        None,
                        token=token,
                        chat_id=chat_id,
                        project_id=project_id,
                        update_id=update_id,
                        summary=summary or "",
                        reason=reason,
                        risk_level=risk_level,
                        evidence_refs=evidence_refs,
                        proposed_changes=proposed_changes,
                        assessment=assessment,
                        policy_version=policy_version,
                        source_ref=source_ref,
                        **email_kw,
                    )
                )
            elif mode == "auto_applied_undo":
                result["value"] = asyncio.run(
                    _push_auto_applied_undo_card(
                        None,
                        token=token,
                        chat_id=chat_id,
                        project_id=project_id,
                        update_id=update_id,
                        summary=summary or "",
                        reason=reason,
                        risk_level=risk_level,
                        evidence_refs=evidence_refs,
                        proposed_changes=proposed_changes,
                        applied_fields=applied_fields,
                        **email_kw,
                    )
                )
            else:
                result["value"] = asyncio.run(
                    _push_applied_update(
                        None,
                        token=token,
                        chat_id=chat_id,
                        project_id=project_id,
                        update_id=update_id,
                        changes_applied=changes_applied,
                    )
                )
        except BaseException as exc:  # pragma: no cover - defensive bridge
            error.append(exc)

    thread = threading.Thread(target=_runner, daemon=True, name="tg-review-push")
    thread.start()
    thread.join()
    if error:
        raise error[0]
    return result["value"]
