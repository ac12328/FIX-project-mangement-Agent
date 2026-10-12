"""Core data operations for the secure draft workspace."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from datetime import datetime, timezone
from typing import Any

from config.settings import CONFIG
from src.email.html_sanitizer import html_to_text, plain_text_to_html, sanitize_email_html
from src.email.recipients import (
    canonical_email,
    envelope_to_legacy,
    normalize_envelope,
    parse_recipient_list,
)
from src.email.sending import email_config_dict, resolve_sender


class WorkspaceConflictError(RuntimeError):
    def __init__(self, snapshot: dict[str, Any]):
        super().__init__("Draft revision conflict")
        self.snapshot = snapshot


class WorkspaceLockedError(RuntimeError):
    pass


def utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def sender_identities(email_config: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    config = email_config if email_config is not None else email_config_dict(CONFIG)
    rows: list[dict[str, Any]] = []
    for item in config.get("sender_identities") or []:
        address = canonical_email(item.get("email_address"))
        if not address:
            continue
        rows.append(
            {
                "identity_id": item.get("identity_id") or address,
                "display_name": item.get("display_name") or address,
                "email_address": address,
                "send_method": item.get("send_method") or config.get("backend", "smtp"),
                "scope": item.get("scope") or "default",
                "project_id": item.get("project_id"),
                "is_default": bool(item.get("is_default") or item.get("scope") == "default"),
            }
        )
    if not rows:
        try:
            identity = resolve_sender(None, None, config)
        except ValueError:
            identity = None
        if identity:
            rows.append(
                {
                    "identity_id": identity.identity_id,
                    "display_name": identity.display_name,
                    "email_address": canonical_email(identity.email_address),
                    "send_method": identity.send_method,
                    "scope": identity.scope,
                    "project_id": identity.project_id,
                    "is_default": identity.is_default,
                }
            )
    return rows


def initialize_draft_workspace(
    conn: sqlite3.Connection,
    draft_id: str,
    *,
    actor_user_id: str = "system:migration",
    create_initial_checkpoint: bool = True,
    commit: bool = True,
    attachment_data_dir: str | None = None,
) -> dict[str, Any]:
    row = conn.execute("SELECT * FROM drafts WHERE draft_id=?", (draft_id,)).fetchone()
    if not row:
        raise ValueError("Draft not found")
    draft = dict(row)
    if draft.get("workspace_updated_at"):
        return draft

    contact_lookup = _contact_lookup(conn, draft.get("project_id"))
    recipients = normalize_envelope(
        {
            "to": parse_recipient_list(
                draft.get("to_email"), source="project_contact", contact_lookup=contact_lookup
            ),
            "cc": parse_recipient_list(
                draft.get("cc"), source="thread_participant", contact_lookup=contact_lookup
            ),
            "bcc": [],
        }
    )
    legacy_body = draft.get("current_draft_text") or ""
    body_html = sanitize_email_html(draft.get("body_html")) if draft.get("body_html") else plain_text_to_html(legacy_body)
    body_text = html_to_text(body_html) or legacy_body
    delivery = _infer_delivery_context(conn, draft, recipients)
    identities = sender_identities()
    sender_identity_id = draft.get("sender_identity_id") or (
        next((item["identity_id"] for item in identities if item["is_default"]), None)
        or (identities[0]["identity_id"] if identities else None)
    )
    attachment_hash = compute_attachment_hash(
        draft_id, manifest=attachment_manifest(draft_id, data_dir=attachment_data_dir),
    )
    now = utcnow_iso()
    conn.execute(
        """UPDATE drafts
           SET body_html=?, body_text=?, recipients_json=?, sender_identity_id=?,
               delivery_mode=?, reply_to_email_id=?, suggested_reply_to_email_id=?,
               owner_user_id=COALESCE(owner_user_id, ?),
               workspace_revision=COALESCE(workspace_revision, 1),
               attachment_hash=?, delivery_status=CASE
                   WHEN status='sent' THEN 'sent'
                   WHEN status='send_failed' THEN 'failed'
                   ELSE COALESCE(delivery_status, 'editing') END,
               workspace_updated_at=?
           WHERE draft_id=?""",
        (
            body_html,
            body_text,
            json.dumps(recipients, ensure_ascii=False),
            sender_identity_id,
            delivery["delivery_mode"],
            delivery["reply_to_email_id"],
            delivery["suggested_reply_to_email_id"],
            actor_user_id,
            attachment_hash,
            now,
            draft_id,
        ),
    )
    snapshot = build_workspace_snapshot(
        conn, draft_id, initialize=False, attachment_data_dir=attachment_data_dir,
    )
    send_hash = compute_send_affecting_hash(snapshot)
    conn.execute(
        "UPDATE drafts SET send_affecting_hash=? WHERE draft_id=?",
        (send_hash, draft_id),
    )
    if create_initial_checkpoint:
        existing = conn.execute(
            "SELECT 1 FROM draft_checkpoints WHERE draft_id=? LIMIT 1", (draft_id,)
        ).fetchone()
        if not existing:
            create_checkpoint(
                conn,
                draft_id,
                actor_user_id=actor_user_id,
                reason="migration_initial",
                commit=False,
                attachment_data_dir=attachment_data_dir,
            )
    if commit:
        conn.commit()
    return dict(conn.execute("SELECT * FROM drafts WHERE draft_id=?", (draft_id,)).fetchone())


def backfill_draft_workspaces(conn: sqlite3.Connection) -> None:
    """Backfill legacy drafts as one migration transaction."""
    for row in conn.execute("SELECT draft_id FROM drafts ORDER BY created_at").fetchall():
        initialize_draft_workspace(
            conn,
            row["draft_id"],
            create_initial_checkpoint=True,
            commit=False,
        )
    _migrate_legacy_schedules(conn)
    conn.commit()


def build_workspace_snapshot(
    conn: sqlite3.Connection,
    draft_id: str,
    *,
    initialize: bool = True,
    commit_initialization: bool = True,
    attachment_data_dir: str | None = None,
) -> dict[str, Any]:
    row = conn.execute("SELECT * FROM drafts WHERE draft_id=?", (draft_id,)).fetchone()
    if not row:
        raise ValueError("Draft not found")
    draft = dict(row)
    if initialize and not draft.get("workspace_updated_at"):
        draft = initialize_draft_workspace(
            conn, draft_id, commit=commit_initialization,
            attachment_data_dir=attachment_data_dir,
        )
    recipients = normalize_envelope(draft.get("recipients_json"))
    attachments = (
        attachment_manifest(draft_id) if attachment_data_dir is None
        else attachment_manifest(draft_id, data_dir=attachment_data_dir)
    )
    return {
        "draft_id": draft_id,
        "project_id": draft.get("project_id"),
        "task_type": draft.get("task_type"),
        "content": {
            "subject": draft.get("subject") or "",
            "body_html": draft.get("body_html") or plain_text_to_html(draft.get("current_draft_text")),
            "body_text": draft.get("body_text") or draft.get("current_draft_text") or "",
        },
        "envelope": {
            "sender_identity_id": draft.get("sender_identity_id"),
            "to": recipients["to"],
            "cc": recipients["cc"],
            "bcc": recipients["bcc"],
        },
        "delivery_context": {
            "delivery_mode": draft.get("delivery_mode") or "unresolved",
            "reply_to_email_id": draft.get("reply_to_email_id"),
            "suggested_reply_to_email_id": draft.get("suggested_reply_to_email_id"),
            "context_packet_ref": draft.get("context_packet_ref"),
        },
        "collaboration": {
            "owner_user_id": draft.get("owner_user_id"),
            "reviewer_user_id": draft.get("reviewer_user_id"),
            "review_state": draft.get("review_state") or "not_requested",
            "review_requested_by": draft.get("review_requested_by"),
            "review_requested_at": draft.get("review_requested_at"),
            "approval_hash": draft.get("approval_hash"),
            "approved_at": draft.get("approved_at"),
        },
        "revision": {
            "number": int(draft.get("workspace_revision") or 1),
            "checkpoint_version": int(draft.get("checkpoint_version") or 0),
            "send_affecting_hash": draft.get("send_affecting_hash"),
            "attachment_hash": compute_attachment_hash(draft_id, manifest=attachments),
        },
        "delivery_status": draft.get("delivery_status") or "editing",
        "legacy_status": draft.get("status"),
        "attachments": attachments,
        "created_at": draft.get("created_at"),
        "updated_at": draft.get("workspace_updated_at"),
    }


def compute_send_affecting_hash(snapshot: dict[str, Any]) -> str:
    payload = {
        "project_id": snapshot.get("project_id"),
        "content": snapshot.get("content"),
        "envelope": snapshot.get("envelope"),
        "delivery_context": snapshot.get("delivery_context"),
        "attachments": [
            {"filename": a.get("filename"), "size": a.get("size"), "sha256": a.get("sha256")}
            for a in snapshot.get("attachments") or []
        ],
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def patch_workspace(
    conn: sqlite3.Connection,
    draft_id: str,
    patch: dict[str, Any],
    *,
    expected_revision: int,
    actor_user_id: str,
    lease_token: str | None = None,
    commit: bool = True,
    email_config: dict[str, Any] | None = None,
    attachment_data_dir: str | None = None,
) -> dict[str, Any]:
    snapshot = build_workspace_snapshot(
        conn, draft_id, commit_initialization=commit,
        attachment_data_dir=attachment_data_dir,
    )
    revision = snapshot["revision"]["number"]
    if revision != expected_revision:
        raise WorkspaceConflictError(snapshot)
    _assert_editable(conn, snapshot, actor_user_id, lease_token)

    content = dict(snapshot["content"])
    envelope = dict(snapshot["envelope"])
    delivery_context = dict(snapshot["delivery_context"])
    collaboration = dict(snapshot["collaboration"])
    changed = False

    if "content" in patch:
        incoming = patch.get("content") or {}
        if "subject" in incoming:
            requested_subject = str(incoming.get("subject") or "")
            if requested_subject != content["subject"]:
                content["subject"] = requested_subject
                changed = True
        if "body_html" in incoming:
            requested_html = sanitize_email_html(incoming.get("body_html"))
            requested_text = html_to_text(requested_html)
            if requested_html != content["body_html"] or requested_text != content["body_text"]:
                content["body_html"] = requested_html
                content["body_text"] = requested_text
                changed = True
        elif "body_text" in incoming:
            requested_text = str(incoming.get("body_text") or "")
            requested_html = plain_text_to_html(requested_text)
            if requested_text != content["body_text"] or requested_html != content["body_html"]:
                content["body_text"] = requested_text
                content["body_html"] = requested_html
                changed = True

    if "envelope" in patch:
        incoming = patch.get("envelope") or {}
        if "sender_identity_id" in incoming:
            requested = incoming.get("sender_identity_id")
            allowed = {item["identity_id"] for item in sender_identities(email_config)}
            if requested not in allowed:
                raise ValueError("Sender identity is not allowlisted")
            if requested != envelope.get("sender_identity_id"):
                envelope["sender_identity_id"] = requested
                changed = True
        for bucket in ("to", "cc", "bcc"):
            if bucket in incoming:
                normalized = normalize_envelope({bucket: incoming[bucket]})[bucket]
                if normalized != envelope.get(bucket, []):
                    envelope[bucket] = normalized
                    changed = True
        envelope = {**normalize_envelope(envelope), "sender_identity_id": envelope.get("sender_identity_id")}

    if "delivery_context" in patch:
        incoming = patch.get("delivery_context") or {}
        mode = incoming.get("delivery_mode", delivery_context["delivery_mode"])
        if mode not in {"unresolved", "new", "reply"}:
            raise ValueError("Invalid delivery mode")
        reply_id = incoming.get("reply_to_email_id", delivery_context.get("reply_to_email_id"))
        if mode == "new":
            reply_id = None
        if mode == "reply" and not reply_id:
            raise ValueError("Reply mode requires a confirmed reply email")
        if mode != delivery_context.get("delivery_mode") or reply_id != delivery_context.get("reply_to_email_id"):
            delivery_context.update({"delivery_mode": mode, "reply_to_email_id": reply_id})
            changed = True

    if "collaboration" in patch:
        incoming = patch.get("collaboration") or {}
        for key in ("owner_user_id", "reviewer_user_id"):
            if key in incoming and incoming.get(key) != collaboration.get(key):
                collaboration[key] = incoming.get(key)
                changed = True

    if not changed:
        return get_workspace(conn, draft_id)

    recipients = normalize_envelope(envelope)
    to_email, cc, _bcc = envelope_to_legacy(recipients)
    now = utcnow_iso()
    next_revision = revision + 1
    prior_review_state = collaboration.get("review_state") or "not_requested"
    next_review_state = "stale" if prior_review_state == "approved" else prior_review_state
    if prior_review_state == "requested":
        raise WorkspaceLockedError("Draft is read-only while review is requested")
    conn.execute(
        """UPDATE drafts SET
               subject=?, body_html=?, body_text=?, current_draft_text=?,
               sender_identity_id=?, recipients_json=?, to_email=?, cc=?,
               delivery_mode=?, reply_to_email_id=?, suggested_reply_to_email_id=?,
               owner_user_id=?, reviewer_user_id=?, review_state=?,
               approval_hash=CASE WHEN ?='stale' THEN NULL ELSE approval_hash END,
               approved_at=CASE WHEN ?='stale' THEN NULL ELSE approved_at END,
               workspace_revision=?, workspace_updated_at=?, delivery_status='editing',
               scheduled_send_at=NULL
           WHERE draft_id=?""",
        (
            content["subject"],
            content["body_html"],
            content["body_text"],
            content["body_text"],
            envelope.get("sender_identity_id"),
            json.dumps(recipients, ensure_ascii=False),
            to_email,
            cc,
            delivery_context["delivery_mode"],
            delivery_context.get("reply_to_email_id"),
            delivery_context.get("suggested_reply_to_email_id"),
            collaboration.get("owner_user_id"),
            collaboration.get("reviewer_user_id"),
            next_review_state,
            next_review_state,
            next_review_state,
            next_revision,
            now,
            draft_id,
        ),
    )
    fresh = build_workspace_snapshot(
        conn, draft_id, initialize=False, attachment_data_dir=attachment_data_dir,
    )
    send_hash = compute_send_affecting_hash(fresh)
    conn.execute(
        "UPDATE drafts SET send_affecting_hash=?, attachment_hash=? WHERE draft_id=?",
        (send_hash, fresh["revision"]["attachment_hash"], draft_id),
    )
    audit_event(conn, draft_id, "workspace_autosaved", actor_user_id, {"revision": next_revision})
    if commit:
        conn.commit()
    return get_workspace(conn, draft_id)


# Compatibility re-exports keep existing callers stable while the implementation
# remains split into focused modules.
from src.email.workspace_support import (
    _assert_editable,
    _contact_lookup,
    _infer_delivery_context,
    _migrate_legacy_schedules,
    attachment_manifest,
    audit_event,
    compute_attachment_hash,
    prepare_attachment_mutation,
    refresh_attachment_revision,
)
from src.email.workspace_history import (
    compare_checkpoint,
    create_checkpoint,
    restore_checkpoint,
)
from src.email.workspace_view import (
    delivery_candidates,
    get_workspace,
    project_contacts,
    public_attachment,
    public_lease,
    public_outbox,
    public_preflight,
    public_preview,
    public_workspace_snapshot,
)
