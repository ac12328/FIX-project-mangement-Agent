"""
Support ticket repository.
"""

import re
import sqlite3
from datetime import datetime, timezone

from src.models.enums import BallWith
from src.models.support_ticket import SupportTicket


def _next_ticket_id(conn: sqlite3.Connection) -> str:
    """Generate next ticket ID in format ST-YYYYMMDD-NNN."""
    today = datetime.now(timezone.utc).strftime("%Y%m%d")
    prefix = f"ST-{today}-"
    row = conn.execute(
        "SELECT ticket_id FROM support_tickets WHERE ticket_id LIKE ? ORDER BY ticket_id DESC LIMIT 1",
        (f"{prefix}%",),
    ).fetchone()
    if row is None:
        return f"{prefix}001"
    last_seq = int(row[0].split("-")[-1])
    return f"{prefix}{last_seq + 1:03d}"


def create_ticket(
    conn: sqlite3.Connection,
    title: str,
    category: str,
    *,
    ticket_id: str | None = None,
    severity: str = "medium",
    status: str = "open",
    source_type: str = "email",
    related_project_id: str | None = None,
    counterparty: str | None = None,
    counterparty_org_id: str | None = None,
    reporter_email: str | None = None,
    reporter_contact_id: str | None = None,
    description: str | None = None,
    assigned_team: str | None = None,
    sla_target_hours: float | None = None,
    thread_anchor: str | None = None,
) -> SupportTicket:
    """Create a new support ticket. Returns the created ticket."""
    if ticket_id is None:
        ticket_id = _next_ticket_id(conn)
    now = datetime.now(timezone.utc).isoformat()
    if not counterparty_org_id:
        from src.repositories.party_repo import resolve_counterparty_org_id

        counterparty_org_id = resolve_counterparty_org_id(
            conn, counterparty=counterparty, reporter_email=reporter_email,
        )
    conn.execute(
        """INSERT INTO support_tickets
           (ticket_id, title, category, severity, status, source_type,
            related_project_id, counterparty, counterparty_org_id,
            reporter_email, reporter_contact_id,
            description, assigned_team, sla_target_hours, thread_anchor,
            reported_at, created_at, updated_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (ticket_id, title, category, severity, status, source_type,
         related_project_id, counterparty, counterparty_org_id,
         reporter_email, reporter_contact_id,
         description, assigned_team, sla_target_hours, thread_anchor, now, now, now),
    )
    # Create work_item envelope
    from src.repositories.work_item_repo import create_work_item
    wi_type = "outreach" if category == "outreach" else "support_ticket"
    wi = create_work_item(
        conn, wi_type, title,
        entity_table="support_tickets",
        entity_id=ticket_id,
    )
    conn.execute(
        "UPDATE support_tickets SET work_item_id = ? WHERE ticket_id = ?",
        (wi.work_item_id, ticket_id),
    )
    return get_ticket(conn, ticket_id)  # type: ignore[return-value]


def get_ticket(conn: sqlite3.Connection, ticket_id: str) -> SupportTicket | None:
    """Get a ticket by ID."""
    row = conn.execute(
        "SELECT * FROM support_tickets WHERE ticket_id = ?", (ticket_id,)
    ).fetchone()
    if row is None:
        return None
    return SupportTicket(**dict(row))


def update_ticket_fields(
    conn: sqlite3.Connection,
    ticket_id: str,
    **fields: object,
) -> SupportTicket | None:
    """Update one or more fields on a ticket. Returns updated ticket."""
    allowed = {
        "title", "category", "severity", "status", "source_type",
        "related_project_id", "counterparty", "counterparty_org_id",
        "reporter_email",
        "reporter_contact_id", "description", "root_cause", "resolution",
        "assigned_team", "escalated_to", "resolved_at", "closed_at",
        "sla_target_hours", "resolution_pattern_id", "reply_draft_text",
        "ai_summary_json", "draft_generated_at",
    }
    to_set = {k: v for k, v in fields.items() if k in allowed}
    if not to_set:
        return get_ticket(conn, ticket_id)
    # Re-resolve org when counterparty / reporter changes and org_id not explicit
    if (
        ("counterparty" in to_set or "reporter_email" in to_set)
        and "counterparty_org_id" not in to_set
    ):
        from src.repositories.party_repo import resolve_counterparty_org_id

        existing = get_ticket(conn, ticket_id)
        cp = to_set.get("counterparty")
        if cp is None and existing is not None:
            cp = existing.counterparty
        remail = to_set.get("reporter_email")
        if remail is None and existing is not None:
            remail = existing.reporter_email
        org_id = resolve_counterparty_org_id(
            conn, counterparty=cp if isinstance(cp, str) or cp is None else str(cp),
            reporter_email=remail if isinstance(remail, str) or remail is None else str(remail),
        )
        if org_id:
            to_set["counterparty_org_id"] = org_id
    to_set["updated_at"] = datetime.now(timezone.utc).isoformat()
    set_clause = ", ".join(f"{k} = ?" for k in to_set)
    values = list(to_set.values()) + [ticket_id]
    conn.execute(
        f"UPDATE support_tickets SET {set_clause} WHERE ticket_id = ?",
        values,
    )
    # Sync work_item status on any ticket status change (open/resolved/closed)
    if "status" in to_set:
        row = conn.execute(
            "SELECT work_item_id FROM support_tickets WHERE ticket_id = ?", (ticket_id,)
        ).fetchone()
        wi_id = row[0] if row else None
        if wi_id:
            from src.repositories.work_item_repo import update_work_item_status
            from src.models.mapping import ticket_status_to_work_item_status
            wi_status = ticket_status_to_work_item_status(to_set["status"])
            if hasattr(wi_status, "value"):
                wi_status = wi_status.value
            update_work_item_status(conn, wi_id, wi_status)
    return get_ticket(conn, ticket_id)


# "open" filter should include all active (non-closed) statuses
_ACTIVE_STATUSES = ("open", "investigating", "waiting_external", "waiting_internal")


def get_tickets_by_status(
    conn: sqlite3.Connection, status: str, limit: int = 50
) -> list[SupportTicket]:
    """Get tickets filtered by status.

    The special value ``"open"`` matches all active statuses
    (open, investigating, waiting_external, waiting_internal).
    Any other value is an exact match.
    """
    if status == "open":
        placeholders = ",".join("?" for _ in _ACTIVE_STATUSES)
        rows = conn.execute(
            f"SELECT * FROM support_tickets WHERE status IN ({placeholders}) "
            "ORDER BY reported_at DESC LIMIT ?",
            (*_ACTIVE_STATUSES, limit),
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT * FROM support_tickets WHERE status = ? ORDER BY reported_at DESC LIMIT ?",
            (status, limit),
        ).fetchall()
    return [SupportTicket(**dict(r)) for r in rows]


def get_tickets_by_counterparty(
    conn: sqlite3.Connection, counterparty: str, limit: int = 50
) -> list[SupportTicket]:
    """Get tickets filtered by counterparty."""
    rows = conn.execute(
        "SELECT * FROM support_tickets WHERE counterparty = ? ORDER BY reported_at DESC LIMIT ?",
        (counterparty, limit),
    ).fetchall()
    return [SupportTicket(**dict(r)) for r in rows]


def search_tickets(
    conn: sqlite3.Connection, query: str, limit: int = 20
) -> list[SupportTicket]:
    """Search tickets by title, description, or counterparty (LIKE match)."""
    pattern = f"%{query}%"
    rows = conn.execute(
        """SELECT * FROM support_tickets
           WHERE title LIKE ? OR description LIKE ? OR counterparty LIKE ?
           ORDER BY reported_at DESC LIMIT ?""",
        (pattern, pattern, pattern, limit),
    ).fetchall()
    return [SupportTicket(**dict(r)) for r in rows]


def get_open_tickets_for_project(
    conn: sqlite3.Connection, project_id: str
) -> list[SupportTicket]:
    """Get open/investigating tickets linked to a project."""
    rows = conn.execute(
        """SELECT * FROM support_tickets
           WHERE related_project_id = ? AND status IN ('open', 'investigating')
           ORDER BY reported_at DESC""",
        (project_id,),
    ).fetchall()
    return [SupportTicket(**dict(r)) for r in rows]


def _subject_tokens(subject: str | None) -> set[str]:
    """Meaningful tokens from a subject for merge similarity checks."""
    if not subject:
        return set()
    s = subject.casefold()
    prefix = r"(?:re|fw|fwd|答复|回覆|回复|轉寄|转发)"
    s = re.sub(rf"^\s*(?:(?:{prefix})\s*[:：]\s*)+", "", s, flags=re.I)
    s = re.sub(r"[\s_\-–—/\\|:：;；,，.。()（）\[\]【】]+", " ", s)
    tokens = re.findall(r"[a-z0-9]+|[\u4e00-\u9fff]{2,}", s)
    noise = {
        "the", "and", "for", "with", "from", "your", "please", "help",
        "email", "issue", "project", "fix", "test", "testing", "request",
        "\u5354\u52a9", "\u8bf7\u534f\u52a9", "\u8acb\u5354\u52a9", "\u554f\u984c", "\u95ee\u9898", "\u8acb\u6559", "\u56de\u590d", "\u56de\u8986",
    }
    return {token for token in tokens if len(token) >= 2 and token not in noise}


def canonical_message_id(value: str | None) -> str | None:
    """Normalize Message-ID variants by their stable generated local part."""
    if not value:
        return None
    normalized = value.strip().strip("<>").casefold()
    if not normalized:
        return None
    return normalized.split("@", 1)[0]


def _message_id_candidates(
    in_reply_to: str | None,
    references_chain: str | None,
) -> set[str]:
    values: list[str] = []
    if in_reply_to:
        values.append(in_reply_to)
    if references_chain:
        values.extend(references_chain.replace(",", " ").split())
    return {
        canonical
        for value in values
        if (canonical := canonical_message_id(value)) is not None
    }


def subjects_compatible(a: str | None, b: str | None) -> bool:
    """True when subjects share enough tokens to be the same issue thread."""
    ta, tb = _subject_tokens(a), _subject_tokens(b)
    if not ta or not tb:
        return False
    inter = ta & tb
    if len(inter) >= 2:
        return True
    if len(inter) >= 1 and len(inter) / min(len(ta), len(tb)) >= 0.5:
        return True
    return False


def find_existing_open_ticket(
    conn: sqlite3.Connection,
    reporter_email: str | None,
    counterparty: str | None,
    project_id: str | None,
    *,
    subject: str | None = None,
    category: str | None = None,
) -> SupportTicket | None:
    """Find an open ticket that is a true follow-up of the same issue.

    Match priority (strict — no category-only or project-only merge):
      1. Same reporter_email + active + subject-compatible
      2. Same counterparty + project + active + subject-compatible

    Category identifies an issue type, not an issue instance. It must never merge
    a new root email by itself. Callers without subject/category retain the legacy
    latest-open lookup behavior.
    """
    if reporter_email:
        rows = conn.execute(
            """SELECT * FROM support_tickets
               WHERE reporter_email = ?
                 AND status IN ('open', 'investigating', 'waiting_external', 'waiting_internal')
               ORDER BY updated_at DESC, reported_at DESC LIMIT 8""",
            (reporter_email,),
        ).fetchall()
        for row in rows:
            ticket = SupportTicket(**dict(row))
            if subject is None and category is None:
                # Back-compat callers with no issue context: first open from sender
                return ticket
            if subject and subjects_compatible(subject, ticket.title):
                return ticket

    if counterparty and project_id:
        rows = conn.execute(
            """SELECT * FROM support_tickets
               WHERE counterparty = ? AND related_project_id = ?
                 AND status IN ('open', 'investigating', 'waiting_external', 'waiting_internal')
               ORDER BY updated_at DESC, reported_at DESC LIMIT 8""",
            (counterparty, project_id),
        ).fetchall()
        for row in rows:
            ticket = SupportTicket(**dict(row))
            if subject and subjects_compatible(subject, ticket.title):
                return ticket
            if subject is None and category is None:
                return ticket

    # Intentionally NO project-only fallback.
    return None


def find_ticket_by_thread(
    conn: sqlite3.Connection,
    in_reply_to: str | None,
    references_chain: str | None,
) -> SupportTicket | None:
    """Find an open ticket whose email thread matches the given threading headers.

    Checks two sources:
    1. support_tickets.thread_anchor — the originating email's message_id
    2. processed_emails linked to a ticket — any email_id in the thread
    """
    candidates = _message_id_candidates(in_reply_to, references_chain)
    if not candidates:
        return None

    active_sql = "('open', 'investigating', 'waiting_external', 'waiting_internal')"

    # SQLite has no canonical Message-ID column yet. Candidate sets are tiny, so
    # normalize active ticket anchors and linked email IDs in Python.
    rows = conn.execute(
        f"""SELECT * FROM support_tickets
            WHERE thread_anchor IS NOT NULL AND status IN {active_sql}
            ORDER BY reported_at DESC"""
    ).fetchall()
    for row in rows:
        if canonical_message_id(row["thread_anchor"]) in candidates:
            return SupportTicket(**dict(row))

    rows = conn.execute(
        f"""SELECT st.*, pe.email_id AS matched_email_id
            FROM support_tickets st
            JOIN processed_emails pe ON pe.ticket_id = st.ticket_id
            WHERE st.status IN {active_sql}
            ORDER BY st.reported_at DESC"""
    ).fetchall()
    for row in rows:
        if canonical_message_id(row["matched_email_id"]) in candidates:
            ticket_data = dict(row)
            ticket_data.pop("matched_email_id", None)
            return SupportTicket(**ticket_data)

    return None


def update_ticket_draft(conn: sqlite3.Connection, ticket_id: str, draft_text: str) -> None:
    """Store generated reply draft text on the ticket."""
    now = datetime.now(timezone.utc).isoformat()
    update_ticket_fields(
        conn, ticket_id,
        reply_draft_text=draft_text,
        draft_generated_at=now,
    )
    conn.commit()


def get_ticket_conversation(
    conn: sqlite3.Connection, ticket_id: str
) -> list[dict]:
    """Build a newest-first conversation thread from ticket evidence.

    Returns dicts with: evidence_id, direction, sender_email, sender_name,
    subject, preview, content_text, timestamp, evidence_type, is_latest.
    """
    rows = conn.execute(
        """SELECT te.evidence_id, te.evidence_type, te.content_summary,
                  te.content_text, te.source_ref, te.created_at,
                  pe.sender_email AS pe_sender, pe.subject AS pe_subject
           FROM ticket_evidence te
           LEFT JOIN processed_emails pe ON pe.email_id = te.source_ref
           WHERE te.ticket_id = ?
           ORDER BY te.created_at DESC, te.evidence_id DESC""",
        (ticket_id,),
    ).fetchall()

    from src.support.pm_queue import is_internal_email

    messages: list[dict] = []
    for i, row in enumerate(rows):
        row_d = dict(row)
        summary = row_d["content_summary"] or ""
        etype = row_d["evidence_type"]

        sender_match = re.search(r"from\s+(\S+@\S+?)(?::|$)", summary, re.IGNORECASE)
        sender_email = row_d.get("pe_sender") or (sender_match.group(1) if sender_match else None)

        if summary.lower().startswith("outbound") or is_internal_email(sender_email):
            direction = "outbound"
        elif summary.lower().startswith("inbound") or etype == "email":
            direction = "inbound"
        else:
            direction = "internal"

        content_text = row_d.get("content_text")
        if content_text:
            preview = content_text[:200]
        else:
            preview = re.sub(
                r"^(?:Follow-up|Outbound|Inbound)\s+email(?:\s+from\s+\S+@\S+)?:\s*",
                "", summary, flags=re.IGNORECASE,
            )[:200]

        messages.append({
            "evidence_id": row_d["evidence_id"],
            "direction": direction,
            "sender_email": sender_email,
            "sender_name": None,
            "subject": row_d.get("pe_subject"),
            "preview": preview,
            "content_text": content_text,
            "timestamp": row_d["created_at"] or "",
            "evidence_type": etype,
            "is_latest": i == 0,
        })

    return messages


def derive_ball_state(conn: sqlite3.Connection, ticket_id: str) -> dict:
    """Derive who has the ball for a support ticket.

    Returns canonical BallWith values (Internal|External|Shared|Unknown), never
    legacy ticket tokens (us|client|resolved|unknown).

    Returns:
        {
            "ball_with": str,  # BallWith value
            "since": str | None,
            "last_outbound_summary": str | None,
            "ball_reason": str | None,  # diagnostic reason code
        }
    """
    ticket = get_ticket(conn, ticket_id)
    if not ticket:
        return {
            "ball_with": BallWith.UNKNOWN.value,
            "since": None,
            "last_outbound_summary": None,
            "ball_reason": "missing_ticket",
        }

    status = ticket.status if isinstance(ticket.status, str) else ticket.status.value

    if status in ("resolved", "closed"):
        ts = ticket.resolved_at or ticket.updated_at
        return {
            "ball_with": BallWith.UNKNOWN.value,
            "since": ts.isoformat() if hasattr(ts, "isoformat") else str(ts) if ts else None,
            "last_outbound_summary": None,
            "ball_reason": "status_resolved",
        }

    if status == "waiting_external":
        last_out = _find_last_outbound(conn, ticket_id)
        return {
            "ball_with": BallWith.EXTERNAL.value,
            "since": last_out.get("timestamp") if last_out else None,
            "last_outbound_summary": last_out.get("preview") if last_out else None,
            "ball_reason": "status_waiting_external",
        }

    if status == "waiting_internal":
        ts = ticket.updated_at
        return {
            "ball_with": BallWith.INTERNAL.value,
            "since": ts.isoformat() if hasattr(ts, "isoformat") else str(ts) if ts else None,
            "last_outbound_summary": None,
            "ball_reason": "status_waiting_internal",
        }

    # Fetch all email evidence with sender info so we can determine direction in Python.
    # An email is outbound if: direction='outbound' in processed_emails, OR the sender
    # domain belongs to the company (catches CS replies picked up via IMAP as inbound).
    from src.support.pm_queue import internal_email_domains

    company_domains: set[str] = internal_email_domains()

    rows = conn.execute(
        """SELECT te.content_text, te.created_at,
                  COALESCE(pe.direction, '') AS pe_direction,
                  COALESCE(pe.sender_email, '') AS pe_sender,
                  te.content_summary
           FROM ticket_evidence te
           LEFT JOIN processed_emails pe ON pe.email_id = te.source_ref
           WHERE te.ticket_id = ? AND te.evidence_type = 'email'
           ORDER BY te.created_at DESC, te.evidence_id DESC""",
        (ticket_id,),
    ).fetchall()

    def _is_outbound(row: dict) -> bool:
        if row["pe_direction"] == "outbound":
            return True
        sender = row["pe_sender"] or ""
        if "@" in sender:
            domain = sender.split("@", 1)[1].lower()
            if domain in company_domains:
                return True
        # Fallback: content_summary prefix for evidence without a processed_email link
        if not row["pe_direction"] and row["content_summary"].lower().startswith("outbound"):
            return True
        return False

    last_outbound = next((r for r in rows if _is_outbound(dict(r))), None)
    last_inbound = next((r for r in rows if not _is_outbound(dict(r))), None)

    if not last_outbound and not last_inbound:
        return {
            "ball_with": BallWith.INTERNAL.value,
            "since": None,
            "last_outbound_summary": None,
            "ball_reason": "no_evidence",
        }

    out_ts = dict(last_outbound)["created_at"] if last_outbound else None
    in_ts = dict(last_inbound)["created_at"] if last_inbound else None

    # Ball is External if we sent the most recent email and they haven't replied since
    if out_ts and (in_ts is None or out_ts > in_ts):
        preview = (dict(last_outbound).get("content_text") or "")[:200] or None
        return {
            "ball_with": BallWith.EXTERNAL.value,
            "since": out_ts,
            "last_outbound_summary": preview,
            "ball_reason": "last_outbound",
        }

    return {
        "ball_with": BallWith.INTERNAL.value,
        "since": in_ts,
        "last_outbound_summary": None,
        "ball_reason": "last_inbound",
    }


def _find_last_outbound(conn: sqlite3.Connection, ticket_id: str) -> dict | None:
    """Find the most recent outbound email evidence for a ticket."""
    row = conn.execute(
        """SELECT content_summary, content_text, created_at
           FROM ticket_evidence
           WHERE ticket_id = ? AND evidence_type = 'email'
             AND content_summary LIKE 'Outbound%'
           ORDER BY created_at DESC LIMIT 1""",
        (ticket_id,),
    ).fetchone()
    if not row:
        return None
    d = dict(row)
    return {
        "timestamp": d.get("created_at"),
        "preview": (d.get("content_text") or "")[:200] or None,
    }


def resolve_ticket_envelope(conn: sqlite3.Connection, ticket_id: str) -> dict:
    """Build a reply envelope from the latest inbound email on a support ticket.

    Returns dict with keys: to_email, cc, subject, in_reply_to, references_chain, quoted_body.
    Falls back to ticket.reporter_email and ticket.title if no processed_emails found.
    """
    ticket = get_ticket(conn, ticket_id)

    # Find latest inbound email linked to this ticket
    row = conn.execute(
        """SELECT email_id, sender_email, subject, cc, references_chain, processed_at
           FROM processed_emails
           WHERE ticket_id = ? AND direction = 'inbound'
           ORDER BY processed_at DESC LIMIT 1""",
        (ticket_id,),
    ).fetchone()

    if not row:
        # Fallback: no emails found, use ticket metadata
        subj = ticket.title if ticket else ticket_id
        return {
            "to_email": ticket.reporter_email if ticket else None,
            "cc": None,
            "subject": f"RE: {subj}" if not subj.upper().startswith("RE:") else subj,
            "in_reply_to": None,
            "references_chain": None,
            "quoted_body": "",
        }

    row = dict(row)
    email_id = row["email_id"]
    sender = row["sender_email"]
    subj = row["subject"] or (ticket.title if ticket else "")
    cc = row["cc"]
    refs = row["references_chain"]
    processed_at = row["processed_at"]

    # Avoid double RE: prefix
    if subj.upper().startswith("RE:"):
        reply_subject = subj
    else:
        reply_subject = f"RE: {subj}"

    # Build References chain: existing refs + this email's Message-ID
    if refs:
        full_refs = f"{refs} {email_id}"
    else:
        full_refs = email_id

    # Build Outlook-style quoted body from ticket_evidence content_text
    evidence_row = conn.execute(
        """SELECT content_text, content_summary
           FROM ticket_evidence
           WHERE ticket_id = ? AND source_ref = ?
           LIMIT 1""",
        (ticket_id, email_id),
    ).fetchone()

    quoted_text = ""
    if evidence_row:
        body = evidence_row[0] or evidence_row[1] or ""
        quoted_text = (
            f"-----Original Message-----\n"
            f"From: {sender}\n"
            f"Sent: {processed_at}\n"
            f"Subject: {subj}\n\n"
            f"{body}"
        )

    return {
        "to_email": sender,
        "cc": cc,
        "subject": reply_subject,
        "in_reply_to": email_id,
        "references_chain": full_refs,
        "quoted_body": quoted_text,
    }
