"""Audit and explicitly split support tickets contaminated by unrelated email threads."""

from __future__ import annotations

import sqlite3
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from typing import Any

from src.repositories.ticket_repo import (
    canonical_message_id,
    create_ticket,
    get_ticket,
    subjects_compatible,
    update_ticket_fields,
)


@dataclass(frozen=True)
class ThreadEmail:
    email_id: str
    subject: str | None
    sender_email: str | None
    processed_at: str | None
    direction: str | None
    in_reply_to: str | None
    references_chain: str | None
    thread_fp: str | None


@dataclass(frozen=True)
class ThreadAuditCandidate:
    candidate_id: str
    ticket_id: str
    ticket_title: str
    ticket_category: str
    root_message_id: str
    root_subject: str
    root_sender: str | None
    first_at: str | None
    last_at: str | None
    email_ids: tuple[str, ...]
    evidence_ids: tuple[str, ...]
    flags: tuple[str, ...]

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["email_ids"] = list(self.email_ids)
        data["evidence_ids"] = list(self.evidence_ids)
        data["flags"] = list(self.flags)
        return data


def _root_key(row: sqlite3.Row | dict[str, Any]) -> str | None:
    data = dict(row)
    references = (data.get("references_chain") or "").replace(",", " ").split()
    first_reference = references[0] if references else None
    return canonical_message_id(first_reference or data.get("in_reply_to") or data.get("thread_fp") or data.get("email_id"))


def _ticket_emails(conn: sqlite3.Connection, ticket_id: str) -> list[sqlite3.Row]:
    return conn.execute(
        """SELECT email_id, subject, sender_email, processed_at, direction,
                  in_reply_to, references_chain, thread_fp
           FROM processed_emails
           WHERE ticket_id = ?
           ORDER BY processed_at, email_id""",
        (ticket_id,),
    ).fetchall()


def _group_emails(rows: list[sqlite3.Row]) -> dict[str, list[sqlite3.Row]]:
    groups: dict[str, list[sqlite3.Row]] = {}
    for row in rows:
        key = _root_key(row)
        if key:
            groups.setdefault(key, []).append(row)
    return groups


def audit_ticket_threads(
    conn: sqlite3.Connection,
    *,
    ticket_id: str | None = None,
) -> list[ThreadAuditCandidate]:
    """Return review candidates without mutating data."""
    if ticket_id:
        tickets = conn.execute(
            "SELECT * FROM support_tickets WHERE ticket_id = ?", (ticket_id,),
        ).fetchall()
    else:
        tickets = conn.execute(
            "SELECT * FROM support_tickets ORDER BY created_at, ticket_id"
        ).fetchall()

    candidates: list[ThreadAuditCandidate] = []
    for ticket_row in tickets:
        ticket = dict(ticket_row)
        groups = _group_emails(_ticket_emails(conn, ticket["ticket_id"]))
        if len(groups) <= 1:
            continue
        anchor_key = canonical_message_id(ticket.get("thread_anchor"))
        if anchor_key is None:
            anchor_key = next(iter(groups), None)

        for root_key, emails in groups.items():
            if root_key == anchor_key:
                continue
            root = next(
                (
                    row for row in emails
                    if not row["in_reply_to"] and not (row["references_chain"] or "").strip()
                ),
                emails[0],
            )
            root_subject = (root["subject"] or "").strip()
            flags = ["independent_root"]
            if not subjects_compatible(root_subject, ticket["title"]):
                flags.append("subject_incompatible")
            else:
                # A separately stored root with a compatible subject is usually
                # a forwarding/header rewrite, not a different issue.
                continue
            if str(ticket["category"]) == "other":
                flags.append("category_other")

            evidence_rows = conn.execute(
                f"""SELECT evidence_id FROM ticket_evidence
                    WHERE ticket_id = ? AND source_ref IN ({','.join('?' for _ in emails)})
                    ORDER BY created_at, evidence_id""",
                (ticket["ticket_id"], *(row["email_id"] for row in emails)),
            ).fetchall()
            first_at = min((row["processed_at"] for row in emails if row["processed_at"]), default=None)
            last_at = max((row["processed_at"] for row in emails if row["processed_at"]), default=None)
            candidate_id = f"{ticket['ticket_id']}:{root_key}"
            candidates.append(ThreadAuditCandidate(
                candidate_id=candidate_id,
                ticket_id=ticket["ticket_id"],
                ticket_title=ticket["title"],
                ticket_category=str(ticket["category"]),
                root_message_id=root["email_id"],
                root_subject=root_subject or "(no subject)",
                root_sender=root["sender_email"],
                first_at=first_at,
                last_at=last_at,
                email_ids=tuple(row["email_id"] for row in emails),
                evidence_ids=tuple(row["evidence_id"] for row in evidence_rows),
                flags=tuple(flags),
            ))
    return candidates


def split_ticket_thread(
    conn: sqlite3.Connection,
    *,
    ticket_id: str,
    root_message_id: str,
) -> dict[str, Any]:
    """Split one explicitly selected thread group into a new ticket transactionally."""
    source = get_ticket(conn, ticket_id)
    if source is None:
        raise ValueError(f"Ticket {ticket_id} not found")

    candidates = audit_ticket_threads(conn, ticket_id=ticket_id)
    selected = next(
        (
            candidate for candidate in candidates
            if canonical_message_id(candidate.root_message_id)
            == canonical_message_id(root_message_id)
        ),
        None,
    )
    if selected is None:
        raise ValueError("Selected root is not an independent thread candidate")

    root = conn.execute(
        "SELECT * FROM processed_emails WHERE email_id = ?",
        (selected.root_message_id,),
    ).fetchone()
    if root is None:
        raise ValueError(f"Root email {selected.root_message_id} not found")
    root_data = dict(root)

    description_row = conn.execute(
        "SELECT content_text FROM ticket_evidence WHERE source_ref = ? LIMIT 1",
        (selected.root_message_id,),
    ).fetchone()
    description = description_row[0] if description_row else None

    savepoint = "split_ticket_thread"
    conn.execute(f"SAVEPOINT {savepoint}")
    try:
        destination = create_ticket(
            conn,
            title=(root_data.get("subject") or selected.root_subject)[:200],
            category=str(source.category.value if hasattr(source.category, "value") else source.category),
            severity=str(source.severity.value if hasattr(source.severity, "value") else source.severity),
            source_type="email",
            related_project_id=source.related_project_id,
            counterparty=source.counterparty,
            counterparty_org_id=source.counterparty_org_id,
            reporter_email=root_data.get("sender_email") or source.reporter_email,
            reporter_contact_id=source.reporter_contact_id,
            description=(description or "")[:2000] or None,
            assigned_team=source.assigned_team,
            sla_target_hours=source.sla_target_hours,
            thread_anchor=selected.root_message_id,
        )

        placeholders = ",".join("?" for _ in selected.email_ids)
        conn.execute(
            f"UPDATE processed_emails SET ticket_id = ? WHERE ticket_id = ? AND email_id IN ({placeholders})",
            (destination.ticket_id, ticket_id, *selected.email_ids),
        )
        if selected.evidence_ids:
            evidence_placeholders = ",".join("?" for _ in selected.evidence_ids)
            conn.execute(
                f"UPDATE ticket_evidence SET ticket_id = ? WHERE ticket_id = ? AND evidence_id IN ({evidence_placeholders})",
                (destination.ticket_id, ticket_id, *selected.evidence_ids),
            )

        moved_drafts: list[str] = []
        skipped_drafts: list[str] = []
        draft_rows = conn.execute(
            "SELECT draft_id, context_packet_ref FROM drafts WHERE ticket_id = ?",
            (ticket_id,),
        ).fetchall()
        selected_keys = {canonical_message_id(email_id) for email_id in selected.email_ids}
        for draft in draft_rows:
            context_key = canonical_message_id(draft["context_packet_ref"])
            if context_key and context_key in selected_keys:
                conn.execute(
                    "UPDATE drafts SET ticket_id = ? WHERE draft_id = ?",
                    (destination.ticket_id, draft["draft_id"]),
                )
                moved_drafts.append(draft["draft_id"])
            else:
                skipped_drafts.append(draft["draft_id"])

        now = datetime.now(timezone.utc).isoformat()
        for affected_ticket_id in (ticket_id, destination.ticket_id):
            latest = conn.execute(
                "SELECT MAX(created_at) FROM ticket_evidence WHERE ticket_id = ?",
                (affected_ticket_id,),
            ).fetchone()[0]
            update_ticket_fields(
                conn,
                affected_ticket_id,
                ai_summary_json=None,
                reply_draft_text=None,
                draft_generated_at=None,
            )
            conn.execute(
                "UPDATE support_tickets SET updated_at = ? WHERE ticket_id = ?",
                (latest or now, affected_ticket_id),
            )

        conn.execute(f"RELEASE SAVEPOINT {savepoint}")
    except Exception:
        conn.execute(f"ROLLBACK TO SAVEPOINT {savepoint}")
        conn.execute(f"RELEASE SAVEPOINT {savepoint}")
        raise

    return {
        "source_ticket_id": ticket_id,
        "new_ticket_id": destination.ticket_id,
        "root_message_id": selected.root_message_id,
        "moved_email_ids": list(selected.email_ids),
        "moved_evidence_ids": list(selected.evidence_ids),
        "moved_draft_ids": moved_drafts,
        "skipped_draft_ids": skipped_drafts,
    }
