"""
Telegram persistence helpers.
Wraps raw message storage, linkage, and unified activity logging.
"""

from __future__ import annotations

import json
import sqlite3
import time

from src.repositories.activity_log_repo import insert_activity
from src.repositories.telegram_repo import (
    get_message_context,
    insert_message_object_link,
    insert_telegram_message,
    list_project_telegram_messages,
)


def _json_or_none(value: dict | list | None) -> str | None:
    if value is None:
        return None
    return json.dumps(value, ensure_ascii=False)


def _coerce_message_id(value) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return -int(time.time() * 1000000)


def link_message_object(
    conn: sqlite3.Connection,
    *,
    chat_id: int,
    telegram_message_id: int,
    object_type: str,
    object_ref: str,
    relation_type: str = "primary",
    project_id: str | None = None,
) -> int:
    """Link one Telegram message to one backend object."""
    return insert_message_object_link(
        conn,
        chat_id=chat_id,
        telegram_message_id=telegram_message_id,
        object_type=object_type,
        object_ref=object_ref,
        relation_type=relation_type,
        project_id=project_id,
    )


def record_inbound_message(
    conn: sqlite3.Connection,
    *,
    chat_id: int,
    telegram_message_id: int,
    text: str | None,
    message_kind: str = "text",
    reply_to_telegram_message_id: int | None = None,
    project_id: str | None = None,
    session_id: str | None = None,
    operator_id: str = "default_operator",
    metadata: dict | None = None,
) -> int:
    """Persist one inbound Telegram message and append an activity entry."""
    row_id = insert_telegram_message(
        conn,
        chat_id=chat_id,
        telegram_message_id=telegram_message_id,
        direction="inbound",
        message_kind=message_kind,
        text=text,
        reply_to_telegram_message_id=reply_to_telegram_message_id,
        project_id=project_id,
        session_id=session_id,
        operator_id=operator_id,
        metadata_json=_json_or_none(metadata),
    )
    insert_activity(
        conn,
        actor="operator",
        action_type=f"telegram_{message_kind}_received",
        project_id=project_id,
        surface="telegram",
        object_ref=session_id,
        summary=(text or "")[:200] or f"Telegram {message_kind} received",
        metadata_json=_json_or_none({
            "chat_id": chat_id,
            "telegram_message_id": telegram_message_id,
            "reply_to_telegram_message_id": reply_to_telegram_message_id,
            "session_id": session_id,
            **(metadata or {}),
        }),
    )
    conn.commit()
    return row_id


def record_outbound_message(
    conn: sqlite3.Connection,
    *,
    chat_id: int,
    telegram_message_id,
    text: str | None,
    message_kind: str = "text",
    reply_to_telegram_message_id: int | None = None,
    project_id: str | None = None,
    session_id: str | None = None,
    operator_id: str = "default_operator",
    metadata: dict | None = None,
    object_links: list[dict] | None = None,
    strict_audit: bool = False,
    commit: bool = True,
) -> int:
    """Persist one outbound Telegram message, its object links, and activity."""
    persisted_message_id = _coerce_message_id(telegram_message_id)
    row_id = insert_telegram_message(
        conn,
        chat_id=chat_id,
        telegram_message_id=persisted_message_id,
        direction="outbound",
        message_kind=message_kind,
        text=text,
        reply_to_telegram_message_id=reply_to_telegram_message_id,
        project_id=project_id,
        session_id=session_id,
        operator_id=operator_id,
        metadata_json=_json_or_none(metadata),
    )
    links = object_links or []
    for link in links:
        if not link.get("object_type") or not link.get("object_ref"):
            continue
        link_message_object(
            conn,
            chat_id=chat_id,
            telegram_message_id=persisted_message_id,
            object_type=link["object_type"],
            object_ref=link["object_ref"],
            relation_type=link.get("relation_type", "primary"),
            project_id=link.get("project_id", project_id),
        )
    primary_object = links[0]["object_ref"] if links else session_id
    insert_activity(
        conn,
        actor="system",
        action_type="telegram_message_sent",
        project_id=project_id,
        surface="telegram",
        object_ref=primary_object,
        summary=(text or "")[:200] or "Telegram message sent",
        metadata_json=_json_or_none({
            "chat_id": chat_id,
            "telegram_message_id": persisted_message_id,
            "reply_to_telegram_message_id": reply_to_telegram_message_id,
            "session_id": session_id,
            "message_kind": message_kind,
            **(metadata or {}),
        }),
    )
    if message_kind == "card":
        try:
            from src.repositories.interaction_log_repo import insert_card_push
            insert_card_push(
                conn,
                card_type=message_kind,
                card_profile=(metadata or {}).get("card_profile") or (metadata or {}).get("profile"),
                project_id=project_id,
                telegram_message_id=persisted_message_id,
            )
        except Exception:
            if strict_audit:
                raise
            try:
                conn.rollback()
            except Exception:
                pass  # audit log must never break card delivery
    if commit:
        conn.commit()
    return persisted_message_id


def record_callback_action(
    conn: sqlite3.Connection,
    *,
    chat_id: int,
    telegram_message_id: int,
    callback_data: str,
    project_id: str | None = None,
    session_id: str | None = None,
    operator_id: str = "default_operator",
    object_ref: str | None = None,
    metadata: dict | None = None,
    commit: bool = True,
) -> int:
    """Append a unified activity row for one Telegram callback."""
    row_id = insert_activity(
        conn,
        actor="operator",
        action_type="telegram_callback_received",
        project_id=project_id,
        surface="telegram",
        object_ref=object_ref or session_id,
        summary=callback_data,
        metadata_json=_json_or_none({
            "chat_id": chat_id,
            "telegram_message_id": telegram_message_id,
            "callback_data": callback_data,
            "session_id": session_id,
            "operator_id": operator_id,
            **(metadata or {}),
        }),
    )
    try:
        from src.repositories.interaction_log_repo import record_operator_response
        record_operator_response(
            conn,
            telegram_message_id=telegram_message_id,
            operator_response=callback_data,
        )
    except Exception:
        pass  # audit log must never break callback handling
    if commit:
        conn.commit()
    return row_id


def find_message_context(
    conn: sqlite3.Connection,
    *,
    chat_id: int,
    telegram_message_id: int,
) -> dict | None:
    """Resolve linked backend context for one Telegram message."""
    return get_message_context(
        conn,
        chat_id=chat_id,
        telegram_message_id=telegram_message_id,
    )


def list_project_messages(
    conn: sqlite3.Connection,
    project_id: str,
    *,
    limit: int = 50,
    object_type: str | None = None,
    object_ref: str | None = None,
) -> list[dict]:
    """List persisted Telegram messages for one project."""
    return list_project_telegram_messages(
        conn,
        project_id,
        limit=limit,
        object_type=object_type,
        object_ref=object_ref,
    )
