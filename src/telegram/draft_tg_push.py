"""Push draft review/preview cards after operator chat draft requests."""

from __future__ import annotations

import logging
import re
import sqlite3
from collections.abc import Callable
from typing import Any, Protocol

from src.agent.active_project import get_active_project_id
from src.agent.claims import extract_write_claims
from src.agent.guards import is_draft_request
from src.agent.intent import detect_project_id

logger = logging.getLogger(__name__)

_DRAFT_ID_IN_REPLY_RE = re.compile(r"`?(draft_[a-f0-9]{8,})`?", re.IGNORECASE)


class _SafeSend(Protocol):
    async def __call__(
        self,
        chat_id: int,
        text: str,
        *,
        reply_to_message_id: int | None = None,
        project_id: str | None = None,
        message_kind: str = "text",
        object_links: list[dict] | None = None,
        reply_markup: Any = None,
    ) -> list[int]: ...


def should_auto_push_draft_preview(user_text: str, response_text: str) -> bool:
    """True when the agent turn created or discussed a project email draft."""
    if is_draft_request(user_text):
        return True
    if _DRAFT_ID_IN_REPLY_RE.search(response_text or ""):
        return True
    return any(c.kind == "draft" for c in extract_write_claims(response_text or ""))


def resolve_draft_project_id(
    conn: sqlite3.Connection,
    *,
    user_text: str,
    effective_project_id: str | None,
    chat_id: int,
) -> str | None:
    if effective_project_id:
        return effective_project_id
    pid = detect_project_id(user_text)
    if pid:
        return pid
    return get_active_project_id(conn, chat_id)


def _draft_id_from_reply(response_text: str) -> str | None:
    m = _DRAFT_ID_IN_REPLY_RE.search(response_text or "")
    return m.group(1) if m else None


def _load_draft_for_push(
    conn: sqlite3.Connection,
    *,
    project_id: str | None,
    response_text: str,
) -> dict | None:
    from src.email.draft_session import get_draft, get_pending_drafts

    draft_id = _draft_id_from_reply(response_text)
    if draft_id:
        row = get_draft(conn, draft_id)
        if row and row.get("status") in ("created", "in_review", "approved", "send_failed"):
            return dict(row)

    if not project_id:
        return None
    pending = get_pending_drafts(conn, project_id)
    return dict(pending[0]) if pending else None


async def push_pending_draft_preview(
    *,
    scoped_db_read: Callable[[], Any],
    safe_send: _SafeSend,
    chat_id: int,
    reply_to_message_id: int | None,
    user_text: str,
    response_text: str,
    project_id: str | None,
) -> bool:
    """Send full draft preview (CP) — same content as /pending → DETAILS."""
    if not should_auto_push_draft_preview(user_text, response_text):
        return False

    try:
        with scoped_db_read() as conn:
            pid = resolve_draft_project_id(
                conn, user_text=user_text, effective_project_id=project_id, chat_id=chat_id,
            )
            draft = _load_draft_for_push(conn, project_id=pid, response_text=response_text)
    except Exception:
        logger.exception("draft_tg_push: failed to load pending draft")
        return False

    if not draft:
        return False

    from src.telegram.handlers_draft import _format_preview_response

    preview = _format_preview_response({
        "action": "preview",
        "draft_id": draft["draft_id"],
        "project_id": draft.get("project_id") or pid,
        "preview_subject": draft.get("subject") or "",
        "preview_body": draft.get("current_draft_text") or "",
        "preview_to": draft.get("to_email") or "",
        "preview_cc": draft.get("cc") or "",
        "card_profile": "CP",
    })

    try:
        await safe_send(
            chat_id,
            preview["text"],
            reply_to_message_id=reply_to_message_id,
            project_id=preview.get("project_id"),
            message_kind="card",
            reply_markup=preview.get("reply_markup"),
            object_links=[
                {
                    "object_type": "draft",
                    "object_ref": draft["draft_id"],
                    "project_id": preview.get("project_id"),
                },
            ],
        )
        logger.info(
            "draft_tg_push: sent preview for %s to chat %s",
            draft["draft_id"],
            chat_id,
        )
        return True
    except Exception:
        logger.exception("draft_tg_push: send failed for %s", draft["draft_id"])
        return False