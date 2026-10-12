"""Persisted draft-card binding shared by Telegram preflight and handlers.

A callback's project token is a consistency check, never a selector for the
latest draft. C/D preview cards become CP in place and keep the original link.
"""

from __future__ import annotations

import json
import sqlite3


def _draft_was_edited(draft: dict) -> tuple[str | None, str | None]:
    """Return (original_body, sent_body) when a draft was meaningfully edited; else (None, None)."""
    if not draft.get("draft_version") or draft["draft_version"] <= 1:
        return None, None
    try:
        history = json.loads(draft.get("draft_history") or "[]")
    except Exception:
        return None, None
    if not history:
        return None, None
    original = (history[0].get("body") or "").strip()
    sent = (draft.get("current_draft_text") or "").strip()
    if original and sent and original != sent:
        return original, sent
    return None, None


def _resolve_card_draft(
    conn: sqlite3.Connection,
    project_id: str | None,
    message_context: dict | None,
) -> tuple[dict | None, str | None]:
    """Resolve only the draft linked to this card, never a project's latest draft.

    A CP preview edits the original C/D card in place, so its persisted link
    (rather than its current button profile) is the durable draft identity.
    """
    from src.email.draft_session import get_draft
    from src.telegram._cb_helpers import extract_message_metadata

    if not message_context:
        return None, project_id
    links = [
        link for link in message_context.get("links", [])
        if link.get("object_type") == "draft" and link.get("object_ref")
    ]
    if len(links) != 1:
        return None, project_id
    link = links[0]
    metadata = extract_message_metadata(message_context)
    if metadata.get("card_profile") not in (None, "C", "D", "CP"):
        return None, project_id
    if metadata.get("draft_id") not in (None, link["object_ref"]):
        return None, project_id
    if message_context.get("direction") not in (None, "outbound") or (
        message_context.get("message_kind") not in (None, "card")
    ):
        return None, project_id

    draft = get_draft(conn, link["object_ref"])
    if not draft or draft.get("status") not in (
        "created", "in_review", "approved", "send_failed",
    ):
        return None, project_id
    draft_project = draft.get("project_id")
    if project_id is not None and project_id != draft_project:
        return None, project_id
    if link.get("project_id") is not None and link["project_id"] != draft_project:
        return None, project_id
    if message_context.get("project_id") is not None and (
        message_context["project_id"] != draft_project
    ):
        return None, project_id
    return draft, draft_project


def _resolve_style_pref_draft(
    conn: sqlite3.Connection,
    callback_entity: str | None,
    message_context: dict | None,
) -> tuple[dict | None, str | None]:
    """Bind a preference choice to the sent, edited draft on this card."""
    from src.email.draft_session import get_draft
    from src.telegram._cb_helpers import extract_message_metadata

    draft_id, separator, project_id = (callback_entity or "").partition(":")
    if not separator or not draft_id or not project_id or not message_context:
        return None, project_id or None
    links = [
        link for link in message_context.get("links", [])
        if link.get("object_type") == "draft" and link.get("object_ref")
    ]
    if len(links) != 1 or links[0]["object_ref"] != draft_id:
        return None, project_id
    metadata = extract_message_metadata(message_context)
    if metadata.get("card_profile") not in (None, "C", "D", "CP") or (
        metadata.get("draft_id") not in (None, draft_id)
    ):
        return None, project_id
    if message_context.get("direction") not in (None, "outbound") or (
        message_context.get("message_kind") not in (None, "card")
    ):
        return None, project_id
    draft = get_draft(conn, draft_id)
    if not draft or draft.get("project_id") != project_id or (
        draft.get("status") != "sent"
    ) or _draft_was_edited(draft)[0] is None:
        return None, project_id
    if links[0].get("project_id") not in (None, project_id) or (
        message_context.get("project_id") not in (None, project_id)
    ):
        return None, project_id
    return draft, project_id

