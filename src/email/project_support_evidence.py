"""Mirror project-related support/internal mail without granting truth-write authority."""

from __future__ import annotations

from datetime import datetime, timezone

from src.email.ops_mail_guard import should_block_project_truth_from_email
from src.email.project_tag_link import resolve_project_id_from_subject
from src.project_reconciliation.contracts import ReconciliationResult
from src.repositories.activity_log_repo import insert_activity
from src.repositories.evidence_repo import insert_evidence


def capture_project_support_evidence(conn, fetched, event) -> bool:
    """Store evidence before ticket routing can short-circuit. Never change state."""
    if event.classification_type not in {"internal", "project_support", "support_issue"}:
        return False
    if getattr(event, "requires_operator_triage", False):
        return False
    project_id = event.project_id
    # An explicit, unique subject identity is required for this secondary route.
    if not project_id or resolve_project_id_from_subject(conn, fetched.subject) != project_id:
        return False
    project = conn.execute(
        "SELECT project_status FROM projects WHERE project_id=?", (project_id,),
    ).fetchone()
    if not project or project[0] not in {"Active", "Waiting", "Blocked"}:
        return False
    if should_block_project_truth_from_email(
        fetched.subject, project_id=project_id, body=fetched.body,
    ):
        return False
    if conn.execute(
        "SELECT 1 FROM evidence_store WHERE project_id=? AND source_ref=? AND evidence_type='email'",
        (project_id, fetched.email_id),
    ).fetchone():
        return False
    insert_evidence(
        conn, project_id=project_id, evidence_type="email",
        content_summary=f"Project support correspondence: {fetched.subject[:180]}",
        content_text=fetched.body,
        source_ref=fetched.email_id,
        evidence_at=fetched.received_at or datetime.now(timezone.utc).isoformat(),
    )
    insert_activity(
        conn, actor="email", surface="email", project_id=project_id,
        action_type="email_project_evidence_added", object_ref=fetched.email_id,
        summary="Support/internal correspondence linked as project evidence; state unchanged.",
    )
    return True


def preview_project_support_email(event, *, chat_id: str) -> ReconciliationResult | None:
    """Use the existing policy/review gate, never the auto-applying email agent."""
    from src.db.connection import scoped_write
    from src.project_reconciliation.service import reconcile_project_from_emails

    if event.classification_type not in {"internal", "project_support", "support_issue"}:
        return
    if not event.project_id or getattr(event, "requires_operator_triage", False):
        return
    with scoped_write() as conn:
        evidence = conn.execute(
            "SELECT 1 FROM evidence_store WHERE project_id=? AND source_ref=? AND evidence_type='email'",
            (event.project_id, event.email_id),
        ).fetchone()
        project = conn.execute(
            "SELECT operator_id, project_status FROM projects WHERE project_id=?", (event.project_id,),
        ).fetchone()
        if not evidence or not project or project[1] not in {"Active", "Waiting", "Blocked"}:
            return
        return reconcile_project_from_emails(
            conn, project_id=event.project_id, mode="preview",
            operator_id=project[0], chat_id=str(chat_id), trusted_surface="email",
            trusted_explicit_operator=False,
            request_key=f"email-support-preview:{event.project_id}:{event.email_id}",
            trigger_kind="email", create_if_missing=False,
        )
