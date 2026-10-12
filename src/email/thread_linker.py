"""
Thread-based email-to-ticket linking.

When an email arrives whose threading headers (In-Reply-To / References)
match an existing open support ticket, this module auto-links it as
evidence and publishes an SSE event so the dashboard refreshes live.
"""

import logging
import sqlite3

from config.defaults import EMAIL_BODY_STORE_CHARS
from src.email.inbox_fetcher import FetchedEmail
from src.events.bus import Event, event_bus
from src.models.support_ticket import SupportTicket
from src.repositories.ticket_evidence_repo import insert_ticket_evidence
from src.repositories.ticket_repo import find_ticket_by_thread

logger = logging.getLogger(__name__)


def try_thread_link(
    conn: sqlite3.Connection,
    fetched: FetchedEmail,
    *,
    is_outbound: bool = False,
    defer_commit: bool = False,
) -> SupportTicket | None:
    """Check if a fetched email belongs to an existing ticket's thread.

    If matched:
    - Links ``processed_emails.ticket_id``
    - Inserts ticket evidence (which also busts the AI summary cache)
    - Publishes a ``ticket_updated`` SSE event unless the caller owns commit

    With ``defer_commit=True``, the caller must commit and call
    ``publish_thread_linked`` only after that commit succeeds. The legacy
    default retains the entry point's commit and publication behavior.
    Returns the matched SupportTicket or None.
    """
    if not fetched.in_reply_to and not fetched.references:
        return None

    ticket = find_ticket_by_thread(conn, fetched.in_reply_to, fetched.references)
    if not ticket:
        return None

    # Repair missing scope only when the original and incoming subjects agree.
    from src.email.project_tag_link import resolve_project_id_from_subject
    from src.repositories.ticket_repo import update_ticket_fields

    project_id = resolve_project_id_from_subject(conn, fetched.subject)
    if project_id and not ticket.related_project_id:
        anchor = conn.execute(
            "SELECT pe.subject, pe.project_id FROM processed_emails pe "
            "JOIN support_tickets st ON st.thread_anchor=pe.email_id WHERE st.ticket_id=?",
            (ticket.ticket_id,),
        ).fetchone()
        if (anchor and resolve_project_id_from_subject(conn, anchor[0]) == project_id
                and anchor[1] in (None, project_id)):
            ticket = update_ticket_fields(conn, ticket.ticket_id, related_project_id=project_id)

    # Skip duplicate linking — evidence already recorded for this Message-ID
    existing_evidence = conn.execute(
        "SELECT 1 FROM ticket_evidence WHERE source_ref = ?",
        (fetched.email_id,),
    ).fetchone()
    if existing_evidence:
        logger.debug(
            "Email %s already linked to ticket evidence — skipping re-notification",
            fetched.email_id,
        )
        return None

    # Link the processed_email row
    conn.execute(
        "UPDATE processed_emails SET ticket_id = ? WHERE email_id = ?",
        (ticket.ticket_id, fetched.email_id),
    )

    # Determine direction label — include sender for at-a-glance identification
    label_prefix = "Outbound" if is_outbound else "Follow-up"
    sender_tag = f" from {fetched.sender_email}" if fetched.sender_email else ""
    label = f"{label_prefix} email{sender_tag}: {fetched.subject[:120]}"

    # Insert evidence (side-effect: busts ai_summary_json cache)
    insert_ticket_evidence(
        conn,
        ticket_id=ticket.ticket_id,
        evidence_type="email",
        content_summary=label,
        content_text=(fetched.body or "")[:EMAIL_BODY_STORE_CHARS],
        source_ref=fetched.email_id,
    )
    if not defer_commit:
        conn.commit()
        publish_thread_linked(ticket.ticket_id)

    logger.info(
        "Thread-linked email %s → ticket %s (%s)",
        fetched.email_id, ticket.ticket_id, label_prefix.lower(),
    )
    return ticket


def publish_thread_linked(ticket_id: str) -> None:
    """Notify ticket readers only after the link transaction has committed."""
    try:
        event_bus.publish(Event(
            event_type="ticket_updated",
            detail={"ticket_id": ticket_id},
            surface="email",
        ))
    except Exception:
        logger.warning("Unable to publish ticket link for %s", ticket_id, exc_info=True)


def backfill_unlinked_emails(conn: sqlite3.Connection) -> int:
    """One-time backfill: scan processed_emails with no ticket_id and try to
    link them to open tickets via threading headers.

    Returns the number of emails successfully linked.
    """
    rows = conn.execute(
        """SELECT email_id, subject, sender_email, direction,
                  in_reply_to, references_chain
           FROM processed_emails
           WHERE ticket_id IS NULL
             AND (in_reply_to IS NOT NULL OR references_chain IS NOT NULL)"""
    ).fetchall()

    linked = 0
    for row in rows:
        email_id, subject, sender_email, direction, in_reply_to, refs = row
        ticket = find_ticket_by_thread(conn, in_reply_to, refs)
        if not ticket:
            continue

        # Link the row
        conn.execute(
            "UPDATE processed_emails SET ticket_id = ? WHERE email_id = ?",
            (ticket.ticket_id, email_id),
        )

        is_outbound = (direction or "").lower() == "outbound"
        label_prefix = "Outbound" if is_outbound else "Follow-up"
        sender_tag = f" from {sender_email}" if sender_email else ""
        label = f"{label_prefix} email{sender_tag}: {(subject or '')[:120]}"

        # Check if evidence already exists to avoid duplicates
        existing = conn.execute(
            "SELECT 1 FROM ticket_evidence WHERE source_ref = ?",
            (email_id,),
        ).fetchone()
        if not existing:
            insert_ticket_evidence(
                conn,
                ticket_id=ticket.ticket_id,
                evidence_type="email",
                content_summary=label,
                source_ref=email_id,
            )

        linked += 1
        logger.info(
            "Backfill-linked email %s → ticket %s (%s)",
            email_id, ticket.ticket_id, label_prefix.lower(),
        )

    if linked:
        conn.commit()
        # Single SSE push for all affected tickets
        ticket_ids = {r[0] for r in conn.execute(
            """SELECT DISTINCT ticket_id FROM processed_emails
               WHERE ticket_id IS NOT NULL
                 AND email_id IN ({})""".format(
                ",".join("?" for _ in rows)
            ),
            [r[0] for r in rows],
        ).fetchall()}
        for tid in ticket_ids:
            try:
                event_bus.publish(Event(
                    event_type="ticket_updated",
                    detail={"ticket_id": tid},
                    surface="backfill",
                ))
            except Exception:
                pass

    logger.info("Backfill complete: %d/%d emails linked", linked, len(rows))
    return linked
