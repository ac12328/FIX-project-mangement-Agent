"""Notify the PM when a FIX project first reaches M8_GoLive.

Persists a project-chat message, activity and configured Telegram delivery
intent in the same transaction as the milestone change. A separate worker
claims committed deliveries after the write lock is released.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import sqlite3
import threading
from typing import Any
from uuid import uuid4

from src.models.enums import Milestone
from src.repositories.activity_log_repo import insert_activity
from src.repositories.assistant_repo import create_thread, find_project_thread
from src.repositories.project_repo import get_project
from src.utils.push_guard import URGENCY_URGENT

logger = logging.getLogger(__name__)

GO_LIVE_ACTION_TYPE = "go_live_ready_notice"
GO_LIVE_NOTICE_KIND = "go_live_ready"
GO_LIVE_LEGACY_SOURCE = "go_live_ready"

_MESSAGE_TEMPLATE = (
    'Hi team, the "{client_name}", "{b2b_code}" FIX project is completed the PCT '
    "and ready to go live. please arrange with the client the \n"
    "1. Contact list\n"
    "2. Soft launch date"
)


def build_go_live_ready_message(*, client_name: str | None, b2b_code: str | None) -> str:
    """Render the go-live reminder with project identity filled in."""
    name = (client_name or "").strip() or "client"
    code = (b2b_code or "").strip() or ""
    return _MESSAGE_TEMPLATE.format(client_name=name, b2b_code=code)


def notice_already_recorded(conn: sqlite3.Connection, project_id: str) -> bool:
    row = conn.execute(
        """SELECT 1 FROM activity_log
           WHERE project_id = ? AND action_type = ?
           LIMIT 1""",
        (project_id, GO_LIVE_ACTION_TYPE),
    ).fetchone()
    return row is not None


def telegram_notice_already_sent(conn: sqlite3.Connection, project_id: str) -> bool:
    row = conn.execute(
        """SELECT 1 FROM telegram_messages
           WHERE project_id = ?
             AND direction = 'outbound'
             AND json_extract(metadata_json, '$.notice') = ?
           LIMIT 1""",
        (project_id, GO_LIVE_NOTICE_KIND),
    ).fetchone()
    return row is not None


def record_go_live_notice(
    conn: sqlite3.Connection,
    project_id: str,
    *,
    actor: str = "system",
    surface: str = "system",
) -> dict[str, Any]:
    """Persist the Dashboard project-chat message and activity entry once.

    Safe to call inside the milestone write transaction. Does not send Telegram.
    """
    if notice_already_recorded(conn, project_id):
        return {"recorded": False, "reason": "already_recorded"}

    project = get_project(conn, project_id)
    if project is None:
        return {"recorded": False, "reason": "project_not_found"}

    text = build_go_live_ready_message(
        client_name=project.client_name,
        b2b_code=project.broker_code or project.project_id,
    )
    owners = _notice_owners(conn, project.operator_id)
    thread_ids: list[str] = []
    for owner in owners:
        thread_id = _ensure_project_thread(
            conn,
            owner_user_id=owner,
            project_id=project_id,
            title=_thread_title(project_id, project.client_name),
        )
        if thread_id:
            _save_notice_message(
                conn,
                thread_id=thread_id,
                owner_user_id=owner,
                project_id=project_id,
                text=text,
            )
            thread_ids.append(thread_id)

    insert_activity(
        conn,
        actor="system",
        action_type=GO_LIVE_ACTION_TYPE,
        project_id=project_id,
        surface=surface,
        object_ref=project_id,
        summary=text,
        metadata_json=json.dumps(
            {
                "notice": GO_LIVE_NOTICE_KIND,
                "source_actor": actor,
                "owners": owners,
                "thread_ids": thread_ids,
            },
            ensure_ascii=False,
        ),
    )
    # The delivery intent is in the same transaction as the milestone and chat
    # notice. A configured send must not disappear if the process exits now.
    from src.services.go_live_delivery import enqueue_go_live_telegram

    enqueue_go_live_telegram(conn, project_id, text)
    return {
        "recorded": True,
        "text": text,
        "owners": owners,
        "thread_ids": thread_ids,
    }


def schedule_go_live_telegram(project_id: str) -> None:
    """Wake the durable outbox after commit; periodic polling survives this thread."""
    if not _telegram_configured():
        return
    thread = threading.Thread(
        target=_push_go_live_telegram_worker,
        args=(project_id,),
        daemon=True,
        name="m8-go-live-tg",
    )
    thread.start()


def notify_project_reached_go_live(
    conn: sqlite3.Connection,
    project_id: str,
    *,
    actor: str = "system",
    surface: str = "system",
    schedule_telegram: bool = True,
) -> dict[str, Any]:
    """Record the notice and delivery intent on *conn* without committing."""
    result = record_go_live_notice(
        conn, project_id, actor=actor, surface=surface,
    )
    # Keep schedule_telegram for compatibility. The caller owns the commit;
    # only the periodic worker may observe its committed delivery intent.
    return result


def push_go_live_telegram_sync(
    conn: sqlite3.Connection | None,
    project_id: str,
    text: str,
    *,
    chat_id: int | None = None,
    expected_bot_fingerprint: str | None = None,
) -> dict[str, Any]:
    """Send the go-live reminder to Telegram. Never raises."""
    from config.settings import CONFIG
    from src.telegram.card_push import _run_push_in_thread
    from src.utils.async_helpers import has_running_loop
    from src.utils.notification_budget import consume_notification, should_push_with_budget

    token = CONFIG.telegram.bot_token
    fallback = list(getattr(CONFIG.telegram, "allowed_chat_ids", []) or [])
    configured_chat_id = CONFIG.telegram.chat_id or (fallback[0] if fallback else 0)
    resolved_chat_id = chat_id or configured_chat_id
    if expected_bot_fingerprint and chat_id is not None and int(configured_chat_id or 0) != int(chat_id):
        return {"sent": False, "error": "recipient_changed"}
    if not token or not resolved_chat_id:
        return {"sent": False, "error": "telegram_not_configured"}
    if expected_bot_fingerprint and hashlib.sha256(token.encode()).hexdigest() != expected_bot_fingerprint:
        return {"sent": False, "error": "bot_identity_changed"}
    if conn is not None:
        if telegram_notice_already_sent(conn, project_id):
            return {"sent": False, "error": "already_sent"}
        if not should_push_with_budget(conn, URGENCY_URGENT):
            return {"sent": False, "error": "budget_exhausted"}

    async def _send(_ignored_conn: sqlite3.Connection | None) -> dict[str, Any]:
        from telegram import Bot

        from src.telegram.runtime import send_persisted_message

        bot = Bot(token=token)
        message_ids = await send_persisted_message(
            bot,
            None,
            chat_id=int(resolved_chat_id),
            text=text,
            project_id=project_id,
            message_kind="text",
            metadata={"notice": GO_LIVE_NOTICE_KIND},
            # A formatting retry after a lost acknowledgement can duplicate an
            # M8 notice. Send plain once and require a durable positive receipt.
            format_mode="plain",
            require_persistence=True,
        )
        mid = message_ids[-1] if message_ids else None
        return {"sent": type(mid) is int and mid > 0, "telegram_message_id": mid}

    try:
        if has_running_loop():
            result = _run_push_in_thread(_send)
        else:
            result = asyncio.run(_send(None))
    except Exception:
        logger.exception("Telegram go-live notice failed for %s", project_id)
        return {"sent": False, "error": "send_failed"}

    if result.get("sent") and conn is not None:
        try:
            consume_notification(conn)
        except Exception:
            logger.exception("Failed to consume notification budget after go-live push")
    return result


def _push_go_live_telegram_worker(project_id: str) -> None:
    from src.services.go_live_delivery import deliver_go_live_telegram

    try:
        deliver_go_live_telegram(project_id)
    except Exception:
        logger.exception("Go-live Telegram outbox worker failed for %s", project_id)


def _telegram_configured() -> bool:
    try:
        from config.settings import CONFIG

        token = CONFIG.telegram.bot_token
        fallback = list(getattr(CONFIG.telegram, "allowed_chat_ids", []) or [])
        chat_id = CONFIG.telegram.chat_id or (fallback[0] if fallback else 0)
        return bool(token and chat_id)
    except Exception:
        return False


def _milestone_value(project: Any) -> str:
    ms = getattr(project, "current_milestone", None)
    if ms is None:
        return ""
    return str(getattr(ms, "value", ms))


def _thread_title(project_id: str, client_name: str | None) -> str:
    name = (client_name or "").strip()
    if name:
        return f"{project_id} {name} go-live ready"
    return f"{project_id} go-live ready"


def _notice_owners(conn: sqlite3.Connection, project_operator_id: str | None) -> list[str]:
    owners: list[str] = []
    if project_operator_id:
        owners.append(str(project_operator_id))
    try:
        from config.settings import CONFIG

        configured = str(getattr(CONFIG, "operator_id", None) or "").strip()
        if configured:
            owners.append(configured)
    except Exception:
        pass
    try:
        rows = conn.execute(
            "SELECT user_id FROM dashboard_users WHERE status = 'active'"
        ).fetchall()
        owners.extend(str(row[0]) for row in rows if row and row[0])
    except sqlite3.OperationalError:
        pass

    unique: list[str] = []
    seen: set[str] = set()
    for owner in owners:
        if owner and owner not in seen:
            seen.add(owner)
            unique.append(owner)
    return unique or ["default_operator"]


def _ensure_project_thread(
    conn: sqlite3.Connection,
    *,
    owner_user_id: str,
    project_id: str,
    title: str,
) -> str | None:
    existing = find_project_thread(
        conn, owner_user_id=owner_user_id, project_id=project_id,
    )
    if existing and list(existing.get("project_ids") or []) == [project_id]:
        return str(existing["thread_id"])
    created = create_thread(
        conn,
        owner_user_id=owner_user_id,
        title=title,
        scope_mode="projects",
        project_ids=[project_id],
    )
    return str(created["thread_id"]) if created else None


def _save_notice_message(
    conn: sqlite3.Connection,
    *,
    thread_id: str,
    owner_user_id: str,
    project_id: str,
    text: str,
) -> None:
    legacy_id = f"{project_id}:{owner_user_id}"
    existing = conn.execute(
        """SELECT message_id FROM assistant_messages
           WHERE legacy_source = ? AND legacy_source_id = ?""",
        (GO_LIVE_LEGACY_SOURCE, legacy_id),
    ).fetchone()
    if existing:
        return
    parts = [{"type": "text", "text": text}]
    conn.execute(
        """INSERT INTO assistant_messages (
               message_id, thread_id, role, content, parts_json,
               legacy_source, legacy_source_id
           ) VALUES (?, ?, 'assistant', ?, ?, ?, ?)""",
        (
            f"amsg_{uuid4().hex}",
            thread_id,
            text,
            json.dumps(parts, ensure_ascii=False),
            GO_LIVE_LEGACY_SOURCE,
            legacy_id,
        ),
    )
    conn.execute(
        "UPDATE assistant_threads SET updated_at=datetime('now') WHERE thread_id=?",
        (thread_id,),
    )
