"""Chat tool handlers for draft operations — extracted from chat_handlers.py."""

from __future__ import annotations

import sqlite3


def chat_list_drafts(conn: sqlite3.Connection, args: dict) -> dict:
    """List drafts, optionally filtered by project and status."""
    from src.services.draft_service import list_drafts

    project_id = args.get("project_id")
    status = args.get("status")
    limit = min(int(args.get("limit", 10)), 20)
    entries = list_drafts(conn, project_id, status=status, limit=limit)
    if not entries:
        return {"text": "No drafts found."}
    lines = [f"Drafts ({len(entries)}):"]
    for d in entries:
        lines.append(
            f"  [{d['draft_id']}] {d.get('project_id', '?')} | "
            f"{d.get('task_type', '?')} | status={d.get('status', '?')} | "
            f"v{d.get('draft_version', 1)} | {d.get('created_at', '?')[:16]}"
        )
    return {"text": "\n".join(lines)}


def chat_get_draft(conn: sqlite3.Connection, args: dict) -> dict:
    """Get a single draft including body text."""
    from src.services.draft_service import get_draft

    draft_id = args.get("draft_id", "")
    if not draft_id:
        return {"text": "Error: draft_id is required."}
    d = get_draft(conn, draft_id)
    if not d:
        return {"text": f"Draft {draft_id} not found."}
    body = d.get("current_draft_text", "") or ""
    return {
        "text": (
            f"Draft: {d['draft_id']}\n"
            f"Project: {d.get('project_id', '?')}\n"
            f"Type: {d.get('task_type', '?')} | Status: {d.get('status', '?')}\n"
            f"To: {d.get('to_email', '?')}\n"
            f"Version: {d.get('draft_version', 1)}\n"
            f"---\n{body[:2000]}"
        )
    }


def chat_edit_draft(conn: sqlite3.Connection, args: dict) -> dict:
    """Edit an existing draft's body, subject, recipient, or CC."""
    from src.email.draft_session import edit_draft, get_draft

    draft_id = args.get("draft_id", "")
    if not draft_id:
        return {"text": "Error: draft_id is required."}

    d = get_draft(conn, draft_id)
    if not d:
        return {"text": f"Draft {draft_id} not found."}
    if d["status"] not in ("created", "in_review"):
        return {"text": f"Cannot edit draft in '{d['status']}' state."}

    new_body = args.get("new_body")
    new_subject = args.get("new_subject")
    if new_body is not None or new_subject is not None:
        ok = edit_draft(conn, draft_id, new_subject=new_subject, new_body=new_body)
        if not ok:
            return {"text": f"Failed to edit draft {draft_id} (max versions reached or invalid state)."}

    # Direct updates for to_email / cc
    direct_updates = {}
    if args.get("to_email") is not None:
        from src.email.draft_safety import (
            is_standalone_trusted_internal_handoff,
            validate_recipient_for_project,
        )
        ok, error, _basis = validate_recipient_for_project(
            conn,
            project_id=d.get("project_id"),
            to_email=args["to_email"],
            allow_trusted_internal_handoff=is_standalone_trusted_internal_handoff(
                d,
                to_email=args["to_email"],
            ),
        )
        if not ok:
            return {"text": f"Error: {error}"}
        direct_updates["to_email"] = args["to_email"]
        context_ref = d.get("context_packet_ref")
        if context_ref:
            source = conn.execute(
                "SELECT sender_email FROM processed_emails WHERE email_id = ?",
                (context_ref,),
            ).fetchone()
            source_email = (source["sender_email"] if source else "") or ""
            if source_email.strip().lower() != str(args["to_email"]).strip().lower():
                direct_updates["context_packet_ref"] = None
                direct_updates["cc"] = None
    if args.get("cc") is not None:
        direct_updates["cc"] = args["cc"]
    if direct_updates:
        set_clause = ", ".join(f"{k} = ?" for k in direct_updates)
        conn.execute(
            f"UPDATE drafts SET {set_clause} WHERE draft_id = ?",
            (*direct_updates.values(), draft_id),
        )
        conn.commit()

    updated = get_draft(conn, draft_id)
    return {
        "text": (
            f"Draft {draft_id} updated (v{updated.get('draft_version', 1)}).\n"
            f"To: {updated.get('to_email', '?')}\n"
            f"Subject: {updated.get('subject', '?')}\n"
            f"---\n{(updated.get('current_draft_text') or '')[:500]}"
        )
    }


def chat_discard_draft(conn: sqlite3.Connection, args: dict) -> dict:
    """Discard a pending draft."""
    from src.email.draft_session import get_draft
    from src.services.draft_service import draft_discard

    draft_id = args.get("draft_id", "")
    if not draft_id:
        return {"text": "Error: draft_id is required."}

    d = get_draft(conn, draft_id)
    if not d:
        return {"text": f"Draft {draft_id} not found."}

    if d["status"] not in ("created", "in_review", "approved", "send_failed"):
        return {"text": f"Cannot discard draft {draft_id} (status: {d['status']})."}

    draft_discard(conn, draft_id, actor="operator", surface="chat")
    return {"text": f"Draft {draft_id} discarded."}
