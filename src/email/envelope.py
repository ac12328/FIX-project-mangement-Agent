"""
Envelope Resolution — resolve recipient, CC, and threading metadata for drafts.

The harness resolves WHO to send to and WHAT thread to continue.
The model decides WHAT to say.
"""

import sqlite3
from dataclasses import dataclass

from src.email.sender_trust import is_internal_company_sender
from src.repositories.contact_repo import get_primary_contact_for_project


@dataclass
class DraftEnvelope:
    """All metadata the harness resolves before the LLM drafts the email body."""
    to_email: str | None = None
    contact_name: str | None = None
    cc: str | None = None
    reply_to_email_id: str | None = None
    references: str | None = None
    original_subject: str | None = None


def resolve_draft_envelope(
    conn: sqlite3.Connection, project_id: str, *, for_reply: bool = False
) -> DraftEnvelope:
    """
    Resolve the full draft envelope for a project.

    Args:
        conn:       SQLite connection
        project_id: Target project
        for_reply:  If True, reply to the sender of the latest inbound email
                    (not the project's primary contact). Use for inbound_reply signals.

    Resolution order when for_reply=False (proactive emails):
        1. Contact lookup → display_name, primary_email
        2. Latest inbound email → threading headers, sender fallback
    Resolution order when for_reply=True (reply to specific email):
        1. Latest inbound email → sender_email as to_email, threading headers
        2. Contact lookup → fallback only if no inbound email found
    """
    env = DraftEnvelope()

    # 1. Contact lookup
    contact_id = get_primary_contact_for_project(conn, project_id)
    contact_email: str | None = None
    contact_name: str | None = None
    if contact_id:
        row = conn.execute(
            "SELECT display_name, primary_email FROM contacts WHERE contact_id = ?",
            (contact_id,),
        ).fetchone()
        if row:
            contact_name = row["display_name"]
            contact_email = row["primary_email"]

    # 2. Latest inbound email for threading + sender
    email_row = conn.execute(
        """SELECT email_id, subject, sender_email, references_chain, cc
           FROM processed_emails
           WHERE project_id = ? AND direction = 'inbound'
           ORDER BY processed_at DESC LIMIT 1""",
        (project_id,),
    ).fetchone()

    if email_row:
        env.reply_to_email_id = email_row["email_id"]
        env.original_subject = email_row["subject"]
        env.references = email_row["references_chain"]
        env.cc = email_row["cc"]

    latest_sender = (email_row["sender_email"] or "").strip() if email_row else None
    env.to_email, env.contact_name = _resolve_to_email(
        conn,
        project_id,
        for_reply=for_reply,
        contact_email=contact_email,
        contact_name=contact_name,
        latest_sender=latest_sender,
    )

    return env


def _project_prefers_external_recipient(conn: sqlite3.Connection, project_id: str) -> bool:
    """Client-facing projects should not draft replies to internal PM forwards."""
    row = conn.execute(
        "SELECT ball_with FROM projects WHERE project_id = ?",
        (project_id,),
    ).fetchone()
    if row is None:
        return True
    return (row["ball_with"] or "").strip() in {"External", "Shared", "Unknown", ""}


def _latest_external_inbound_sender(
    conn: sqlite3.Connection, project_id: str
) -> str | None:
    rows = conn.execute(
        """SELECT sender_email
           FROM processed_emails
           WHERE project_id = ? AND direction = 'inbound'
           ORDER BY processed_at DESC""",
        (project_id,),
    ).fetchall()
    for row in rows:
        sender = (row["sender_email"] or "").strip()
        if sender and not is_internal_company_sender(sender):
            return sender
    return None


def _resolve_to_email(
    conn: sqlite3.Connection,
    project_id: str,
    *,
    for_reply: bool,
    contact_email: str | None,
    contact_name: str | None,
    latest_sender: str | None,
) -> tuple[str | None, str | None]:
    prefer_external = _project_prefers_external_recipient(conn, project_id)

    if for_reply and latest_sender and (not prefer_external or not is_internal_company_sender(latest_sender)):
        return latest_sender, _name_from_email(latest_sender)
    if contact_email and (not prefer_external or not is_internal_company_sender(contact_email)):
        return contact_email, contact_name
    if latest_sender and prefer_external and is_internal_company_sender(latest_sender):
        external_sender = _latest_external_inbound_sender(conn, project_id)
        if external_sender:
            return external_sender, _name_from_email(external_sender)
    if latest_sender and not prefer_external:
        return latest_sender, _name_from_email(latest_sender)
    if contact_email:
        return contact_email, contact_name
    if latest_sender and not is_internal_company_sender(latest_sender):
        return latest_sender, _name_from_email(latest_sender)
    # Project client_email is the durable operator-set recipient for proactive drafts
    proj_email, proj_name = _project_client_email(conn, project_id)
    if proj_email and (not prefer_external or not is_internal_company_sender(proj_email)):
        return proj_email, proj_name or contact_name
    external = _latest_external_inbound_sender(conn, project_id)
    if external:
        return external, _name_from_email(external)
    if proj_email:
        return proj_email, proj_name or contact_name
    return None, None


def _project_client_email(
    conn: sqlite3.Connection, project_id: str,
) -> tuple[str | None, str | None]:
    row = conn.execute(
        "SELECT client_email, client_name FROM projects WHERE project_id = ?",
        (project_id,),
    ).fetchone()
    if not row:
        return None, None
    email = (row["client_email"] or "").strip() or None
    name = (row["client_name"] or "").strip() or None
    return email, name


def _name_from_email(email_addr: str) -> str | None:
    """Best-effort first name extraction from email address.

    'Alex.Smith@client.demo.invalid' → 'Alex'
    'asmith@client.demo.invalid' → None (too ambiguous)
    """
    local = email_addr.split("@")[0] if "@" in email_addr else email_addr
    parts = local.replace("_", ".").replace("-", ".").split(".")
    if len(parts) >= 2 and len(parts[0]) >= 3:
        return parts[0].capitalize()
    return None
