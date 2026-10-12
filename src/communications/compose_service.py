"""Application service for project-independent email draft composition."""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Callable, ContextManager, Iterator

from src.email import draft_generator
from src.email.draft_session import DraftGenerationLink, create_draft

WriteConnectionFactory = Callable[[], ContextManager[sqlite3.Connection]]


@dataclass(frozen=True)
class PreparedEmailDraft:
    """Resolved, generated email content that has not been persisted yet."""

    to_email: str
    subject: str
    body: str
    contact_id: str | None = None
    generation_link: DraftGenerationLink | None = None


@dataclass(frozen=True)
class ComposeEmailResult:
    draft_id: str
    to_email: str
    subject: str
    body: str
    contact_id: str | None = None


def prepare_email_draft(
    conn: sqlite3.Connection,
    *,
    context: str,
    to_email: str | None = None,
    contact_id: str | None = None,
    contact_name: str | None = None,
    subject: str | None = None,
    body: str | None = None,
    operator_directive: str | None = None,
    validation_feedback: str | None = None,
) -> PreparedEmailDraft:
    """Resolve and generate a project-independent email without writing it."""
    recipient, resolved_contact_id, resolved_name = resolve_recipient(
        conn,
        to_email=to_email,
        contact_id=contact_id,
        contact_name=contact_name,
    )
    generation_link = None
    if subject and body:
        final_subject = subject.strip()
        final_body = body.strip()
    else:
        generated = draft_generator.generate_adhoc_email_draft(
            to_email=recipient,
            context=context,
            contact_name=resolved_name,
            subject_hint=subject,
            operator_directive=operator_directive,
            validation_feedback=validation_feedback,
        )
        final_subject, final_body, recipient = generated
        trace_id = getattr(generated, "trace_id", None)
        output_revision_id = getattr(generated, "output_revision_id", None)
        if trace_id and output_revision_id:
            generation_link = DraftGenerationLink(trace_id, output_revision_id)
    if not final_subject or not final_body:
        raise ValueError("Draft subject and body must not be empty")
    return PreparedEmailDraft(
        to_email=recipient,
        subject=final_subject,
        body=final_body,
        contact_id=resolved_contact_id,
        generation_link=generation_link,
    )


def persist_prepared_email_draft(
    conn: sqlite3.Connection,
    prepared: PreparedEmailDraft,
    *,
    cc: str | None = None,
    purpose: str = "personal_email",
    related_project_id: str | None = None,
    scheduled_send_at: str | None = None,
    write_connection_factory: WriteConnectionFactory | None = None,
) -> ComposeEmailResult:
    """Persist already prepared email content using a short write scope."""
    with _draft_write_connection(conn, write_connection_factory) as wconn:
        draft_id = create_draft(
            wconn,
            project_id=related_project_id,
            task_type=purpose,
            subject=prepared.subject,
            body=prepared.body,
            to_email=prepared.to_email,
            cc=cc,
            scheduled_send_at=scheduled_send_at,
            generation_link=prepared.generation_link,
        )
    return ComposeEmailResult(
        draft_id=draft_id,
        to_email=prepared.to_email,
        subject=prepared.subject,
        body=prepared.body,
        contact_id=prepared.contact_id,
    )


def compose_email_draft(
    conn: sqlite3.Connection,
    *,
    context: str,
    to_email: str | None = None,
    contact_id: str | None = None,
    contact_name: str | None = None,
    subject: str | None = None,
    body: str | None = None,
    cc: str | None = None,
    purpose: str = "personal_email",
    related_project_id: str | None = None,
    scheduled_send_at: str | None = None,
    operator_directive: str | None = None,
    validation_feedback: str | None = None,
    write_connection_factory: WriteConnectionFactory | None = None,
) -> ComposeEmailResult:
    """Resolve, generate, and persist a reviewable email draft."""
    prepared = prepare_email_draft(
        conn,
        context=context,
        to_email=to_email,
        contact_id=contact_id,
        contact_name=contact_name,
        subject=subject,
        body=body,
        operator_directive=operator_directive,
        validation_feedback=validation_feedback,
    )
    return persist_prepared_email_draft(
        conn,
        prepared,
        cc=cc,
        purpose=purpose,
        related_project_id=related_project_id,
        scheduled_send_at=scheduled_send_at,
        write_connection_factory=write_connection_factory,
    )


@contextmanager
def _draft_write_connection(
    conn: sqlite3.Connection,
    write_connection_factory: WriteConnectionFactory | None,
) -> Iterator[sqlite3.Connection]:
    try:
        row = conn.execute("PRAGMA query_only").fetchone()
        query_only = bool(int(row[0])) if row is not None else False
    except (sqlite3.Error, TypeError, ValueError, IndexError):
        query_only = False
    if not query_only:
        yield conn
        return
    if write_connection_factory is None:
        from src.db.connection import scoped_write

        write_connection_factory = scoped_write
    with write_connection_factory() as wconn:
        yield wconn


def resolve_recipient(
    conn: sqlite3.Connection,
    *,
    to_email: str | None = None,
    contact_id: str | None = None,
    contact_name: str | None = None,
) -> tuple[str, str | None, str | None]:
    """Resolve an explicit address, contact ID, or unambiguous contact name."""
    explicit = (to_email or "").strip()
    if explicit:
        return explicit, contact_id, contact_name
    if contact_id:
        row = conn.execute(
            "SELECT contact_id, display_name, primary_email FROM contacts WHERE contact_id = ?",
            (contact_id,),
        ).fetchone()
        if not row or not row["primary_email"]:
            raise ValueError(f"Contact {contact_id} has no primary email")
        return row["primary_email"], row["contact_id"], row["display_name"]
    name = (contact_name or "").strip()
    if not name:
        raise ValueError("Provide to_email, contact_id, or contact_name")
    rows = conn.execute(
        """SELECT contact_id, display_name, primary_email FROM contacts
           WHERE LOWER(display_name) = LOWER(?) AND primary_email IS NOT NULL""",
        (name,),
    ).fetchall()
    if not rows:
        raise ValueError(f"No contact found with name '{name}'")
    if len(rows) > 1:
        options = ", ".join(
            f"{row['display_name']} <{row['primary_email']}>" for row in rows
        )
        raise ValueError(f"Contact name '{name}' is ambiguous: {options}")
    row = rows[0]
    return row["primary_email"], row["contact_id"], row["display_name"]
