"""
Push helpers for Profile C (Operational Draft) and Profile N (LLM Error) cards.
"""

from __future__ import annotations

import asyncio
import sqlite3
import threading
from datetime import datetime, timezone
from typing import Any

from telegram import Bot

from config.settings import CONFIG
from src.db.connection import scoped_read, scoped_write
from src.telegram.cards import build_card, format_card_text
from src.telegram.i18n import t
from src.telegram.markup import card_to_markup
from src.telegram.runtime import send_persisted_message
from src.utils.notification_budget import (
    should_push_with_budget, consume_notification, reserve_notification_attempt,
)
from src.utils.async_helpers import has_running_loop
from src.utils.push_guard import URGENCY_NORMAL, URGENCY_EMERGENCY


def _run_push_in_thread(coro_factory) -> dict[str, Any]:
    """Run an async push coroutine in a new thread (for when a loop is already running).

    With conn=None, the caller must release its writer lease before entering
    this bridge; send_persisted_message then owns brief persistence writes.
    Explicit caller-owned connections retain their legacy lock contract.
    """
    result: dict[str, Any] = {}
    error: list[BaseException] = []

    def _runner() -> None:
        try:
            result["value"] = asyncio.run(coro_factory(None))
        except BaseException as exc:  # pragma: no cover - defensive bridge
            error.append(exc)

    thread = threading.Thread(target=_runner, daemon=True, name="tg-card-push")
    thread.start()
    thread.join()
    if error:
        raise error[0]
    return result["value"]


# ---------------------------------------------------------------------------
# Profile C — Operational Draft Card
# ---------------------------------------------------------------------------

def push_operational_draft_card_sync(
    conn: sqlite3.Connection | None,
    *,
    draft_id: str,
    to_email: str,
    subject: str,
    chat_id: int | None = None,
) -> dict[str, Any]:
    """Synchronously send a Profile C Operational Draft Card to Telegram."""
    token = CONFIG.telegram.bot_token
    fallback_allowed = list(getattr(CONFIG.telegram, "allowed_chat_ids", []) or [])
    resolved_chat_id = chat_id or CONFIG.telegram.chat_id or (fallback_allowed[0] if fallback_allowed else 0)
    if not token or not resolved_chat_id:
        return {"telegram_message_id": None, "sent": False, "error": "telegram_not_configured"}
    # An owned draft must not hold the serializer across summary/model or
    # Telegram I/O. Reserve its normal-urgency budget slot atomically first.
    if conn is None:
        with scoped_write() as budget_conn:
            allowed = reserve_notification_attempt(budget_conn, URGENCY_NORMAL)
    else:
        # Preserve the existing direct-call, caller-owned budget contract.
        allowed = should_push_with_budget(conn, URGENCY_NORMAL)
    if not allowed:
        return {"telegram_message_id": None, "sent": False, "error": "budget_exhausted"}
    if has_running_loop():
        result = _run_push_in_thread(
            lambda c: _push_operational_draft_card(
                c, token=token, chat_id=resolved_chat_id,
                draft_id=draft_id, to_email=to_email, subject=subject,
            )
        )
    else:
        result = asyncio.run(
            _push_operational_draft_card(
                conn, token=token, chat_id=resolved_chat_id,
                draft_id=draft_id, to_email=to_email, subject=subject,
            )
        )
    if result.get("sent") and conn is not None:
        consume_notification(conn)
    return result



def send_claimed_operational_draft_card_sync(
    *, draft_id: str, to_email: str, subject: str, chat_id: int, token: str,
) -> dict[str, Any]:
    """Send a claimed, budgeted card with mandatory local message audit."""
    def send():
        return _push_operational_draft_card(
            None, token=token, chat_id=chat_id, draft_id=draft_id,
            to_email=to_email, subject=subject, require_persistence=True,
        )

    return _run_push_in_thread(lambda _conn: send()) if has_running_loop() else asyncio.run(send())


def _generate_draft_summary(subject: str, body: str) -> str:
    """Generate a 1-line email purpose summary in the operator's language."""
    from src.llm.client import llm_call
    from src.utils.operator_language import prose_label, read_response_language

    lang = read_response_language()
    label = prose_label(lang)
    if lang == "en":
        system = (
            f"Summarise the purpose of this email in one {label} sentence (≤25 words). "
            "Output only the summary, nothing else."
        )
        user = f"Subject: {subject}\nBody: {body[:400]}"
    else:
        system = (
            f"用一句{label}（≤25字）概括此電郵的目的。只輸出摘要，無其他文字。"
        )
        user = f"主旨：{subject}\n內容：{body[:400]}"
    try:
        result = llm_call(
            "triage",
            [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            temperature=0.1,
        )
        return (result or "").strip()[:80]
    except Exception:
        return ""


async def _push_operational_draft_card(
    conn: sqlite3.Connection | None,
    *,
    token: str,
    chat_id: int,
    draft_id: str,
    to_email: str,
    subject: str,
    require_persistence: bool = False,
) -> dict[str, Any]:
    from src.email.draft_session import get_draft
    if conn is None:
        with scoped_read() as read_conn:
            draft = get_draft(read_conn, draft_id)
    else:
        draft = get_draft(conn, draft_id)
    body_text = (draft.get("current_draft_text") or "") if draft else ""
    project_id = (draft.get("project_id") if draft else None) or None
    summary = _generate_draft_summary(subject, body_text)
    summary_line = f"\n💬 {summary}" if summary else ""
    body = t("push.draft_ready_body").format(
        to_email=to_email, subject=subject, draft_id=draft_id, summary_line=summary_line,
    )
    # Profile D for project drafts (DETAILS uses project_id); C was leaving project_id=None
    # which made DETAILS/SEND resolve to empty pending list ("找不到待處理的草稿").
    profile = "D" if project_id else "C"
    card = build_card(profile, t("push.draft_ready_title"), body, project_id=project_id)
    bot = Bot(token=token)
    message_ids = await send_persisted_message(
        bot, conn,
        chat_id=chat_id,
        text=format_card_text(card),
        project_id=project_id,
        message_kind="card",
        reply_markup=card_to_markup(card),
        object_links=[{
            "object_type": "draft",
            "object_ref": draft_id,
            "project_id": project_id,
        }],
        metadata={"card_profile": profile, "draft_id": draft_id},
        # A claimed outbox send must make exactly one network attempt per chunk.
        # Rich/HTML fallback can duplicate a message after an ambiguous timeout.
        format_mode="plain" if require_persistence else None,
        require_persistence=require_persistence,
    )
    telegram_message_id = message_ids[-1] if message_ids else None
    return {
        "telegram_message_id": telegram_message_id,
        "telegram_message_ids": message_ids,
        "sent": telegram_message_id is not None,
        "sent_at": datetime.now(timezone.utc).isoformat(),
    }


# ---------------------------------------------------------------------------
# Profile N — LLM Error Card
# ---------------------------------------------------------------------------

def push_error_card_sync(
    conn: sqlite3.Connection | None,
    *,
    task: str,
    error_class: str,
    trace_id: str | None = None,
    error_message: str | None = None,
    chat_id: int | None = None,
) -> dict[str, Any]:
    """Synchronously send a Profile N LLM Error Card to Telegram."""
    from src.telegram.error_notices import BACKGROUND_MAINTENANCE, error_notice
    if task in BACKGROUND_MAINTENANCE:
        from src.repositories.digest_queue_repo import enqueue_digest_item

        def enqueue(owned_conn: sqlite3.Connection) -> None:
            enqueue_digest_item(
                owned_conn, category="system_maintenance", title=task,
                body=error_notice(task, error_class, reference=trace_id),
                metadata={"task": task},
                # Caller-owned transactions commit at their own boundary.
                commit=False,
            )

        if conn is None:
            with scoped_write() as owned_conn:
                enqueue(owned_conn)
        else:
            enqueue(conn)
        return {"telegram_message_id": None, "sent": False, "queued": True}
    token = CONFIG.telegram.bot_token
    fallback_allowed = list(getattr(CONFIG.telegram, "allowed_chat_ids", []) or [])
    resolved_chat_id = chat_id or CONFIG.telegram.chat_id or (fallback_allowed[0] if fallback_allowed else 0)
    if not token or not resolved_chat_id:
        return {"telegram_message_id": None, "sent": False, "error": "telegram_not_configured"}
    # Budget gate — error cards are emergency (bypass budget)
    if conn is not None and not should_push_with_budget(conn, URGENCY_EMERGENCY):
        return {"telegram_message_id": None, "sent": False, "error": "budget_exhausted"}
    if has_running_loop():
        result = _run_push_in_thread(
            lambda c: _push_error_card(
                c, token=token, chat_id=resolved_chat_id, task=task,
                error_class=error_class, trace_id=trace_id, error_message=error_message,
            )
        )
    else:
        result = asyncio.run(
            _push_error_card(
                conn, token=token, chat_id=resolved_chat_id, task=task,
                error_class=error_class, trace_id=trace_id, error_message=error_message,
            )
        )
    if result.get("sent"):
        if conn is None:
            with scoped_write() as receipt_conn:
                consume_notification(receipt_conn, commit=False)
        else:
            consume_notification(conn)
    return result


async def _push_error_card(
    conn: sqlite3.Connection | None,
    *,
    token: str,
    chat_id: int,
    task: str,
    error_class: str,
    trace_id: str | None,
    error_message: str | None,
) -> dict[str, Any]:
    bot = Bot(token=token)
    from src.telegram.error_notices import error_notice
    body = error_notice(task, error_class, reference=trace_id)
    card = build_card("N", t("push.error_title"), body)
    message_ids = await send_persisted_message(
        bot, conn,
        chat_id=chat_id,
        text=format_card_text(card),
        project_id=None,
        message_kind="card",
        reply_markup=card_to_markup(card),
        metadata={"card_profile": "N", "task": task, "error_class": error_class, "trace_id": trace_id},
    )
    telegram_message_id = message_ids[-1] if message_ids else None
    return {
        "telegram_message_id": telegram_message_id,
        "sent": telegram_message_id is not None,
        "sent_at": datetime.now(timezone.utc).isoformat(),
    }


# ---------------------------------------------------------------------------
# Profile Q — Commitment Confirmation Card
# ---------------------------------------------------------------------------

def push_commitment_card_sync(
    conn: sqlite3.Connection | None,
    *,
    project_id: str,
    commitment_text: str,
    due_date: str,
    reply_id: str,
    chat_id: int | None = None,
    session_id: str | None = None,
    expected_bot_fingerprint: str | None = None,
) -> dict[str, Any]:
    """Synchronously send a Profile Q Commitment Confirmation Card to Telegram.

    Stores commitment data in a telegram_sessions row (flow_type='commitment_confirm')
    so the CONFIRM handler can retrieve it later.
    """
    from src.telegram.session import create_session, set_session_data

    token = CONFIG.telegram.bot_token
    fallback_allowed = list(getattr(CONFIG.telegram, "allowed_chat_ids", []) or [])
    resolved_chat_id = chat_id or CONFIG.telegram.chat_id or (fallback_allowed[0] if fallback_allowed else 0)
    if not token or not resolved_chat_id:
        return {"telegram_message_id": None, "sent": False, "error": "telegram_not_configured"}
    if expected_bot_fingerprint is not None:
        import hashlib
        if hashlib.sha256(str(token).encode()).hexdigest() != expected_bot_fingerprint:
            return {"telegram_message_id": None, "sent": False, "error": "bot_identity_changed"}
    # The post-commit path opens only short persistence scopes. Network I/O
    # never holds the shared SQLite writer lease on that path.
    if conn is None:
        with scoped_write() as wconn:
            if not should_push_with_budget(wconn, URGENCY_NORMAL):
                return {"telegram_message_id": None, "sent": False, "error": "budget_exhausted"}
            if session_id is None:
                session_id = create_session(
                    wconn, "commitment_confirm", originating_ref=project_id, commit=False,
                )
                set_session_data(wconn, session_id, {
                    "project_id": project_id,
                    "commitment_text": commitment_text,
                    "due_date": due_date,
                    "reply_id": reply_id,
                }, commit=False)
    else:
        if not should_push_with_budget(conn, URGENCY_NORMAL):
            return {"telegram_message_id": None, "sent": False, "error": "budget_exhausted"}
        if session_id is None:
            session_id = create_session(conn, "commitment_confirm", originating_ref=project_id)
            set_session_data(conn, session_id, {
                "project_id": project_id,
                "commitment_text": commitment_text,
                "due_date": due_date,
                "reply_id": reply_id,
            })

    if has_running_loop():
        result = _run_push_in_thread(
            lambda c: _push_commitment_card(
                c, token=token, chat_id=resolved_chat_id,
                project_id=project_id, commitment_text=commitment_text,
                due_date=due_date, session_id=session_id,
            )
        )
    else:
        result = asyncio.run(
            _push_commitment_card(
                conn, token=token, chat_id=resolved_chat_id,
                project_id=project_id, commitment_text=commitment_text,
                due_date=due_date, session_id=session_id,
            )
        )
    if result.get("sent"):
        if conn is None:
            with scoped_write() as wconn:
                consume_notification(wconn, commit=False)
        else:
            consume_notification(conn)
    return result


async def _push_commitment_card(
    conn: sqlite3.Connection,
    *,
    token: str,
    chat_id: int,
    project_id: str,
    commitment_text: str,
    due_date: str,
    session_id: str,
) -> dict[str, Any]:
    bot = Bot(token=token)
    from src.telegram.operator_messages import en
    body = (f"{project_id}\n" + ("Detected commitment: " if en() else "偵測到的承諾：") + commitment_text +
            "\n" + ("Source due date: " if en() else "原文期限：") + (due_date or ("Not specified" if en() else "未提供")) +
            "\n\n" + ("No follow-up has been created by this card. Confirm the commitment and due date before creating one."
                          if en() else "這張卡尚未替你建立跟進。請先核對承諾內容及期限，再確認建立。"))
    title = t("push.commitment_title")
    card = build_card("Q", title, body, project_id=project_id)
    message_ids = await send_persisted_message(
        bot, conn,
        chat_id=chat_id,
        text=format_card_text(card),
        project_id=project_id,
        message_kind="card",
        reply_markup=card_to_markup(card),
        metadata={
            "card_profile": "Q",
            "session_id": session_id,
            "project_id": project_id,
        },
    )
    telegram_message_id = message_ids[-1] if message_ids else None
    return {
        "telegram_message_id": telegram_message_id,
        "sent": telegram_message_id is not None,
        "sent_at": datetime.now(timezone.utc).isoformat(),
    }
