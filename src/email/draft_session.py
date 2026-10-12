"""
Draft Review Flow — DraftSession lifecycle management.
DraftSession: created → in_review → approved → sent / discarded
"""

import json
import logging
import re as _re
import sqlite3
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone

from src.events.bus import Event, event_bus
from src.db.savepoint import savepoint


VALID_STATUSES = {
    "created", "in_review", "approved", "sending", "sent", "discarded", "send_failed",
}
VALID_TRANSITIONS = {
    "created": {"in_review", "discarded"},
    "in_review": {"approved", "discarded", "in_review"},  # in_review→in_review = edit cycle
    "approved": {"sending", "discarded", "send_failed"},
    "sending": {"sent", "send_failed"},
    # Terminal states
    "sent": set(),
    "discarded": set(),
    "send_failed": {"in_review", "discarded"},
}
MAX_VERSIONS = 5


@dataclass(frozen=True)
class DraftGenerationLink:
    """Exact inference identity returned by the draft-generation LLM call."""

    trace_id: str
    output_revision_id: str

_PLACEHOLDER_RE = _re.compile(
    r'\[N/A\]|\$[a-z_]+|\[PLACEHOLDER[^\]]*\]',
    _re.IGNORECASE,
)
_MIN_BODY_LEN = 20
_MAX_BODY_LEN = 5000


def validate_draft_content(
    subject: str,
    body: str,
    *,
    for_send: bool = False,
) -> tuple[bool, str | None]:
    """Validate draft subject and body.

    Creation/review allows [PLACEHOLDER] tags so the operator can fill them in.
    Send-time validation rejects unfilled placeholders.

    Returns (True, None) if valid, or (False, reason_string) if invalid.
    """
    if not subject or not subject.strip():
        return False, "Draft subject is empty."
    stripped = body.strip() if body else ""
    if not stripped:
        return False, "Draft body is empty."
    if len(stripped) < _MIN_BODY_LEN:
        return False, f"Draft body too short ({len(stripped)} chars, minimum {_MIN_BODY_LEN})."
    if len(stripped) > _MAX_BODY_LEN:
        return False, f"Draft body too long ({len(stripped)} chars, maximum {_MAX_BODY_LEN})."
    if for_send:
        match = _PLACEHOLDER_RE.search(body)
        if match:
            return (
                False,
                f"Draft contains unfilled placeholder: '{match.group()}'. "
                "Fill it in before sending.",
            )
    return True, None


def create_draft(
    conn: sqlite3.Connection,
    project_id: str | None,
    task_type: str,
    subject: str,
    body: str,
    *,
    to_email: str | None = None,
    cc: str | None = None,
    context_packet_ref: str | None = None,
    ticket_id: str | None = None,
    work_item_id: str | None = None,
    scheduled_send_at: str | None = None,
    generation_link: DraftGenerationLink | None = None,
    commit: bool = True,
    post_commit: list[Callable[[], None]] | None = None,
    attachment_data_dir: str | None = None,
) -> str:
    """Create a new draft. Returns draft_id.

    work_item_id resolution order:
    1. If work_item_id given directly — use it.
    2. If project_id given — look up projects.work_item_id.
    3. If ticket_id given — look up support_tickets.work_item_id.
    4. None — standalone draft, no work_item link.

    Dedup: if a pending draft (created/in_review) already exists for the same
    project+subject+to_email, return that draft_id instead of creating a new one.

    For a caller-owned transaction, pass ``commit=False`` and ``post_commit``.
    Run the collected effects only after the caller's write scope commits.
    """
    if not commit and post_commit is None:
        raise ValueError("A caller-owned draft transaction requires post_commit effects")

    # Resolve work_item_id if not supplied directly
    if work_item_id is None and project_id is not None:
        row = conn.execute(
            "SELECT work_item_id FROM projects WHERE project_id = ?", (project_id,)
        ).fetchone()
        if row and row[0]:
            work_item_id = row[0]
    if work_item_id is None and ticket_id is not None:
        row = conn.execute(
            "SELECT work_item_id FROM support_tickets WHERE ticket_id = ?", (ticket_id,)
        ).fetchone()
        if row and row[0]:
            work_item_id = row[0]
    # Check for existing pending draft with same key fields
    existing = conn.execute(
        """SELECT draft_id FROM drafts
           WHERE status IN ('created', 'in_review')
             AND project_id IS ?
             AND subject = ?
             AND (to_email IS ? OR (to_email IS NULL AND ? IS NULL))
           LIMIT 1""",
        (project_id, subject, to_email, to_email),
    ).fetchone()
    if existing:
        with savepoint(conn, ensure_transaction=True):
            # Re-drafts must flow through the workspace revision model instead of
            # mutating legacy columns in place.
            from src.email.draft_workspace import initialize_draft_workspace, patch_workspace

            draft_id = existing[0]
            old = get_draft(conn, draft_id) or {}
            if not old.get("workspace_updated_at"):
                initialize_draft_workspace(
                    conn,
                    draft_id,
                    actor_user_id=_draft_operator_id(conn, project_id),
                    commit=False,
                    attachment_data_dir=attachment_data_dir,
                )
                old = get_draft(conn, draft_id) or old

            body_changed = (old.get("current_draft_text") or "") != (body or "")
            subject_changed = (old.get("subject") or "") != (subject or "")
            if body_changed or subject_changed:
                ok = edit_draft(
                    conn,
                    draft_id,
                    new_subject=subject if subject_changed else None,
                    new_body=body if body_changed else None,
                    commit=False,
                    attachment_data_dir=attachment_data_dir,
                )
                if not ok:
                    patch_workspace(
                        conn,
                        draft_id,
                        {"content": {"subject": subject, "body_text": body}},
                        expected_revision=int(old.get("workspace_revision") or 1),
                        actor_user_id="system:legacy-redraft",
                        commit=False,
                        attachment_data_dir=attachment_data_dir,
                    )

            latest = get_draft(conn, draft_id) or old
            envelope_patch: dict[str, str] = {}
            if to_email is not None and to_email != (latest.get("to_email") or ""):
                envelope_patch["to"] = to_email
            if cc is not None and cc != (latest.get("cc") or ""):
                envelope_patch["cc"] = cc
            if envelope_patch:
                patch_workspace(
                    conn,
                    draft_id,
                    {"envelope": envelope_patch},
                    expected_revision=int(latest.get("workspace_revision") or 1),
                    actor_user_id="system:legacy-redraft",
                    commit=False,
                    attachment_data_dir=attachment_data_dir,
                )
        if commit:
            conn.commit()
        return draft_id

    valid, reason = validate_draft_content(subject, body)
    if not valid:
        raise ValueError(f"Draft content invalid: {reason}")

    now = datetime.now(timezone.utc).isoformat()
    draft_id = f"draft_{uuid.uuid4().hex[:12]}"
    draft_history = json.dumps([
        {
            "version": 1,
            "subject": subject,
            "body": body,
            "edited_at": now,
            "source": "agent",
        }
    ])
    with savepoint(conn, ensure_transaction=True):
        conn.execute(
            """INSERT INTO drafts
               (draft_id, project_id, task_type, status, subject,
                current_draft_text, draft_version, draft_history,
                to_email, cc, context_packet_ref, ticket_id, work_item_id,
                scheduled_send_at, created_at)
               VALUES (?, ?, ?, 'created', ?, ?, 1, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (draft_id, project_id, task_type, subject, body, draft_history,
             to_email, cc, context_packet_ref, ticket_id, work_item_id,
             scheduled_send_at, now),
        )
        from src.email.draft_workspace import initialize_draft_workspace

        initialize_draft_workspace(
            conn,
            draft_id,
            actor_user_id=_draft_operator_id(conn, project_id),
            commit=False,
            attachment_data_dir=attachment_data_dir,
        )
        # Domain activity for project timeline (State/Email/Draft/Operator expectation)
        try:
            from src.repositories.activity_log_repo import insert_activity

            to_part = f" → {to_email}" if to_email else ""
            insert_activity(
                conn,
                actor="system",
                action_type="draft_created",
                project_id=project_id,
                surface="telegram",
                object_ref=draft_id,
                summary=f"Draft {draft_id} created{to_part}: {subject[:80]}",
            )
        except Exception:
            pass  # activity must never block draft creation
        _bind_draft_output_revision(
            conn, draft_id, version=1, content=body, generation_link=generation_link,
            commit=False,
        )
    def after_commit() -> None:
        try:
            event_bus.publish(Event(
                event_type="draft_created",
                project_id=project_id,
                detail={"draft_id": draft_id, "task_type": task_type},
                surface="telegram",
            ))
        except Exception:
            logging.getLogger(__name__).exception("Committed draft event publication failed")

    if commit:
        conn.commit()
        after_commit()
    else:
        post_commit.append(after_commit)
    return draft_id


def _draft_operator_id(conn: sqlite3.Connection, project_id: str | None) -> str:
    if project_id:
        row = conn.execute(
            "SELECT operator_id FROM projects WHERE project_id=?", (project_id,),
        ).fetchone()
        if row and row[0]:
            return str(row[0])
    from config.settings import CONFIG
    return CONFIG.operator_id or "default_operator"


def _bind_draft_output_revision(
    conn: sqlite3.Connection, draft_id: str, *, version: int, content: str,
    prior_output_revision_id: str | None = None,
    generation_link: DraftGenerationLink | None = None,
    commit: bool = True,
) -> str:
    """Bind a persisted draft version only to explicitly supplied lineage."""
    from src.memory.output_revision_repo import (
        bind_inference_revision_to_artifact, create_output_revision,
    )
    draft = get_draft(conn, draft_id) or {}
    operator_id = _draft_operator_id(conn, draft.get("project_id"))
    if generation_link:
        revision_id = bind_inference_revision_to_artifact(
            conn, inference_output_revision_id=generation_link.output_revision_id,
            trace_id=generation_link.trace_id, operator_id=operator_id,
            surface="draft", artifact_type="draft", artifact_id=draft_id,
            revision=version, content=content,
        )
    else:
        revision_id = create_output_revision(
            conn, operator_id=operator_id, surface="draft", artifact_type="draft",
            artifact_id=draft_id, revision=version, trace_id=None, content=content,
            prior_output_revision_id=prior_output_revision_id,
        )
    if commit:
        conn.commit()
    return revision_id


def get_draft(conn: sqlite3.Connection, draft_id: str) -> dict | None:
    """Get a draft by ID."""
    row = conn.execute("SELECT * FROM drafts WHERE draft_id = ?", (draft_id,)).fetchone()
    return dict(row) if row else None


def update_draft_status(
    conn: sqlite3.Connection,
    draft_id: str,
    new_status: str,
    *,
    commit: bool = True,
) -> bool:
    """Transition a draft to a new status. Returns True if successful."""
    draft = get_draft(conn, draft_id)
    if draft is None:
        return False

    current = draft["status"]
    allowed = VALID_TRANSITIONS.get(current, set())
    if new_status not in allowed:
        return False

    conn.execute(
        "UPDATE drafts SET status = ? WHERE draft_id = ?",
        (new_status, draft_id),
    )
    conn.execute(
        """INSERT INTO draft_status_history
           (draft_id, from_status, to_status, actor, surface, changed_at)
           VALUES (?, ?, ?, 'system', 'draft_session', datetime('now'))""",
        (draft_id, current, new_status),
    )
    if commit:
        conn.commit()
    return True


def edit_draft(
    conn: sqlite3.Connection,
    draft_id: str,
    new_subject: str | None = None,
    new_body: str | None = None,
    *,
    commit: bool = True,
    attachment_data_dir: str | None = None,
) -> bool:
    """Edit a draft (creates new version). Returns True if successful.

    Edited subject/body must still satisfy the review-time content rules.
    Placeholders remain allowed until send-time validation.
    """
    draft = get_draft(conn, draft_id)
    if draft is None:
        return False

    if draft["status"] not in ("created", "in_review"):
        return False

    version = draft.get("draft_version", 1)
    if version >= MAX_VERSIONS:
        return False

    next_version = version + 1
    updated_subject = new_subject if new_subject is not None else draft.get("subject")
    updated_body = new_body if new_body is not None else draft.get("current_draft_text")
    if (updated_subject or "") == (draft.get("subject") or "") and (
        updated_body or ""
    ) == (draft.get("current_draft_text") or ""):
        return False
    # Preserve legacy short operator edits; send-time validation remains strict.
    # Reject only empty subject/body at edit time.
    if not (updated_subject or "").strip() or not (updated_body or "").strip():
        return False
    draft_history = _parse_draft_history(draft.get("draft_history"))
    if not draft_history:
        draft_history.append(
            {
                "version": version,
                "subject": draft.get("subject"),
                "body": draft.get("current_draft_text"),
                "edited_at": draft.get("created_at") or datetime.now(timezone.utc).isoformat(),
                "source": "agent",
            }
        )
    draft_history.append(
        {
            "version": next_version,
            "subject": updated_subject,
            "body": updated_body,
            "edited_at": datetime.now(timezone.utc).isoformat(),
            "source": "operator",
        }
    )

    if draft.get("workspace_updated_at"):
        from src.email.draft_workspace import patch_workspace

        content_patch: dict[str, str] = {}
        if new_subject is not None:
            content_patch["subject"] = new_subject
        if new_body is not None:
            content_patch["body_text"] = new_body
        patch_workspace(
            conn,
            draft_id,
            {"content": content_patch},
            expected_revision=int(draft.get("workspace_revision") or 1),
            actor_user_id="system:legacy-edit",
            commit=False,
            attachment_data_dir=attachment_data_dir,
        )

    updates = {
        "draft_version": next_version,
        "draft_history": json.dumps(draft_history),
    }
    if new_subject is not None:
        updates["subject"] = new_subject
    if new_body is not None:
        updates["current_draft_text"] = new_body

    set_clause = ", ".join(f"{k} = ?" for k in updates)
    values = list(updates.values()) + [draft_id]
    conn.execute(f"UPDATE drafts SET {set_clause} WHERE draft_id = ?", values)
    prior = conn.execute(
        """SELECT output_revision_id FROM output_revisions
           WHERE surface='draft' AND artifact_type='draft' AND artifact_id=? AND revision=?""",
        (draft_id, version),
    ).fetchone()
    prior_id = prior[0] if prior else None
    if prior_id:
        from src.learning.evaluation.producer_outcomes import attribute_draft_outcome
        attribute_draft_outcome(
            conn, draft_id=draft_id, draft_version=version,
            outcome_type="correction", idempotency_key=f"draft-edit:{draft_id}:{next_version}",
            actor_id="operator",
        )
        from src.memory.output_revision_repo import (
            create_output_revision, get_output_revision, inherit_revision_exposures,
        )
        prior_revision = get_output_revision(conn, prior_id)
        new_revision_id = create_output_revision(
            conn, operator_id=_draft_operator_id(conn, draft.get("project_id")),
            surface="draft", artifact_type="draft", artifact_id=draft_id,
            revision=next_version, trace_id=prior_revision.get("trace_id") if prior_revision else None,
            content=updated_body or "", prior_output_revision_id=prior_id,
        )
        inherit_revision_exposures(
            conn, source_output_revision_id=prior_id,
            target_output_revision_id=new_revision_id,
        )
    else:
        _bind_draft_output_revision(
            conn, draft_id, version=next_version, content=updated_body or "",
            prior_output_revision_id=None, commit=False,
        )
    if commit:
        conn.commit()
    return True


def get_pending_drafts(
    conn: sqlite3.Connection, project_id: str | None = None
) -> list[dict]:
    """Get drafts awaiting review (includes send_failed so operator can retry).

    Returns lightweight rows (no draft_history/body) for list views.
    """
    cols = ("d.draft_id, d.project_id, d.task_type, d.draft_version, d.status, "
            "d.to_email, d.cc, d.subject, d.ticket_id, d.sent_at, d.created_at")
    if project_id:
        rows = conn.execute(
            f"SELECT {cols} FROM drafts d WHERE d.project_id = ? AND d.status IN ('created', 'in_review', 'send_failed') ORDER BY d.created_at DESC",
            (project_id,),
        ).fetchall()
    else:
        from config.defaults import ACTIVE_PROJECT_STATUSES
        placeholders = ",".join("?" for _ in ACTIVE_PROJECT_STATUSES)
        rows = conn.execute(
            f"""SELECT {cols} FROM drafts d
                LEFT JOIN projects p ON d.project_id = p.project_id
                WHERE d.status IN ('created', 'in_review', 'send_failed')
                  AND (p.project_status IN ({placeholders}) OR d.project_id IS NULL)
                ORDER BY d.created_at DESC""",
            ACTIVE_PROJECT_STATUSES,
        ).fetchall()
    return [dict(r) for r in rows]


def _parse_draft_history(raw_value: str | None) -> list[dict]:
    if not raw_value:
        return []
    try:
        parsed = json.loads(raw_value)
    except json.JSONDecodeError:
        return []
    return parsed if isinstance(parsed, list) else []
