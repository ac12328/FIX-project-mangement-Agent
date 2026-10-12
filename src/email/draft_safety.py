"""Deterministic factual safety checks for outbound email drafts."""
from __future__ import annotations

import re
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path

from src.agent.draft_recipient_guard import check_draft_recipient
from src.email.instruction_fidelity import is_trusted_internal_mailbox


@dataclass(frozen=True)
class DraftSafetyIssue:
    code: str
    message: str
    field: str = "body"


@dataclass(frozen=True)
class DraftSafetyResult:
    ok: bool
    issues: list[DraftSafetyIssue] = field(default_factory=list)
    recipient_basis: str | None = None
    attachment_names: tuple[str, ...] = ()

    @property
    def error_summary(self) -> str:
        return "; ".join(issue.message for issue in self.issues)


_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
_IDENTIFIER_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    (
        "SenderID",
        re.compile(r"(?i)\b(?:sender|sendercomp)id\b\s*[:=]\s*[`']?([A-Z0-9._-]{3,})"),
    ),
    (
        "TargetID",
        re.compile(r"(?i)\b(?:target|targetcomp)id\b\s*[:=]\s*[`']?([A-Z0-9._-]{3,})"),
    ),
    (
        "Account",
        re.compile(
            r"(?i)(?:\btag\s*1\b|\baccount(?:\s+code)?\b)"
            r"(?:\s*(?:\([^)]*\))?\s*[:=]\s*|\s+(?:is|must\s+be|should\s+be)\s+)"
            r"[`']?([A-Z0-9._-]{3,})"
        ),
    ),
    (
        "ClOrdID prefix",
        re.compile(
            r"(?i)\b(?:clordid|tag\s*11)\b.{0,100}?\bprefix\b\s*(?:is|of|[:=])?\s*[`']?([A-Z0-9._-]{2,})"
        ),
    ),
)
_FILENAME_RE = re.compile(r"\b([A-Za-z0-9][A-Za-z0-9_. -]+\.(?:pdf|docx|xlsx|csv|txt|xml|cfg|log|zip))\b", re.I)
_ATTACHED_RE = re.compile(r"(?i)\b(?:please\s+find\s+attached|attached\s+(?:is|are|file|document)|in\s+the\s+attached)\b")
_MISSING_ATTACHMENT_RE = re.compile(
    r"(?i)\b(?:please\s+re-?send|attachment\s+(?:was|is|appears?)\s+(?:missing|not\s+included)|confirm\s+whether\s+the\s+attachment)\b"
)
_PRIOR_MESSAGE_RE = re.compile(
    r"(?i)\b(?:thank\s+you\s+for\s+your\s+(?:message|reply|email)|your\s+(?:message|email|reply|attachment)|"
    r"message\s+(?:was|appears?\s+to\s+be)\s+(?:truncated|incomplete)|we\s+received\s+your)\b"
)


def _normalize_email(value: str | None) -> str:
    return (value or "").strip().lower()


def _recipient_allowlist(conn: sqlite3.Connection, project_id: str) -> dict[str, str]:
    allowed: dict[str, str] = {}

    def _add(email: str | None, basis: str) -> None:
        normalized = _normalize_email(email)
        if normalized and _EMAIL_RE.match(normalized):
            allowed.setdefault(normalized, basis)

    row = conn.execute(
        "SELECT client_email FROM projects WHERE project_id = ?", (project_id,)
    ).fetchone()
    if row:
        _add(row["client_email"], "project_client_email")

    try:
        rows = conn.execute(
            """SELECT c.primary_email
               FROM contact_project_links l
               JOIN contacts c ON c.contact_id = l.contact_id
               WHERE l.project_id = ?""",
            (project_id,),
        ).fetchall()
        for contact in rows:
            _add(contact["primary_email"], "linked_contact")
    except sqlite3.OperationalError:
        pass

    try:
        rows = conn.execute(
            "SELECT primary_email, cross_project_history FROM contacts"
        ).fetchall()
        for contact in rows:
            history = contact["cross_project_history"] or ""
            if project_id in history:
                _add(contact["primary_email"], "legacy_project_contact")
    except sqlite3.OperationalError:
        pass

    rows = conn.execute(
        """SELECT sender_email FROM processed_emails
           WHERE project_id = ? AND direction = 'inbound'""",
        (project_id,),
    ).fetchall()
    for email_row in rows:
        _add(email_row["sender_email"], "project_inbound_sender")
    return allowed


def validate_recipient_for_project(
    conn: sqlite3.Connection,
    *,
    project_id: str | None,
    to_email: str | None,
    allow_trusted_internal_handoff: bool = False,
) -> tuple[bool, str | None, str | None]:
    """Validate recipient membership and return (ok, error, basis).

    Trusted internal handoffs are deliberately narrow: callers may opt in, but
    the candidate address must still be one of the canonical shared mailboxes.
    """
    normalized = _normalize_email(to_email)
    if not normalized or not _EMAIL_RE.match(normalized):
        return False, "Draft recipient is missing or invalid.", None
    if not project_id:
        return True, None, "standalone_recipient"

    trusted_internal_handoff = (
        allow_trusted_internal_handoff
        and is_trusted_internal_mailbox(normalized)
    )
    legacy_guard = check_draft_recipient(
        conn,
        project_id=project_id,
        to_email=normalized,
        allow_trusted_internal_handoff=trusted_internal_handoff,
    )
    if not legacy_guard.get("ok"):
        return False, str(legacy_guard.get("error") or "Unsafe recipient."), None

    allowed = _recipient_allowlist(conn, project_id)
    basis = allowed.get(normalized)
    if basis:
        return True, None, basis
    if trusted_internal_handoff:
        return True, None, "trusted_internal_handoff"
    return (
        False,
        f"Recipient '{normalized}' is not linked to project {project_id} or its inbound email history.",
        None,
    )


def is_standalone_trusted_internal_handoff(
    draft: dict,
    *,
    to_email: str | None = None,
) -> bool:
    """Authorize only standalone internal follow-ups to canonical mailboxes."""
    candidate = draft.get("to_email") if to_email is None else to_email
    return (
        draft.get("task_type") == "internal_follow_up"
        and not draft.get("context_packet_ref")
        and is_trusted_internal_mailbox(candidate)
    )


def _authoritative_project_text(conn: sqlite3.Connection, project_id: str) -> str:
    parts: list[str] = []
    project = conn.execute(
        "SELECT * FROM projects WHERE project_id = ?", (project_id,)
    ).fetchone()
    if project:
        parts.extend(str(value) for value in dict(project).values() if value is not None)
    for row in conn.execute(
        """SELECT subject, sender_email, body_snippet, references_chain
           FROM processed_emails WHERE project_id = ?""",
        (project_id,),
    ).fetchall():
        parts.extend(str(value) for value in dict(row).values() if value is not None)
    try:
        for row in conn.execute(
            """SELECT content_summary, content_text, source_ref
               FROM evidence_store WHERE project_id = ?""",
            (project_id,),
        ).fetchall():
            parts.extend(str(value) for value in dict(row).values() if value is not None)
    except sqlite3.OperationalError:
        pass
    try:
        for row in conn.execute(
            "SELECT memory_key, memory_value FROM working_memory WHERE project_id = ?",
            (project_id,),
        ).fetchall():
            parts.extend(str(value) for value in dict(row).values() if value is not None)
    except sqlite3.OperationalError:
        pass
    return "\n".join(parts).lower()


def _extract_identifiers(text: str) -> list[tuple[str, str]]:
    found: list[tuple[str, str]] = []
    for label, pattern in _IDENTIFIER_PATTERNS:
        for match in pattern.finditer(text or ""):
            found.append((label, match.group(1).strip("`'.,;")))
    return found


def _context_email(
    conn: sqlite3.Connection,
    *,
    project_id: str | None,
    context_ref: str | None,
):
    if not context_ref:
        return None
    row = conn.execute(
        """SELECT email_id, project_id, direction, sender_email, subject, body_snippet
           FROM processed_emails WHERE email_id = ?""",
        (context_ref,),
    ).fetchone()
    if not row:
        return None
    if project_id and row["project_id"] != project_id:
        return None
    # Legacy projectless support drafts may reference historical rows that lack
    # direction/project metadata. Project-scoped replies remain strict.
    if project_id and row["direction"] != "inbound":
        return None
    return row


def validate_draft_safety(
    conn: sqlite3.Connection,
    draft: dict,
    *,
    data_dir: str,
    phase: str = "send",
) -> DraftSafetyResult:
    """Validate one draft against project evidence and attachment state."""
    issues: list[DraftSafetyIssue] = []
    project_id = draft.get("project_id")
    body = draft.get("current_draft_text") or ""
    subject = draft.get("subject") or ""
    to_email = draft.get("to_email")

    recipient_ok, recipient_error, recipient_basis = validate_recipient_for_project(
        conn,
        project_id=project_id,
        to_email=to_email,
        allow_trusted_internal_handoff=is_standalone_trusted_internal_handoff(draft),
    )
    if not recipient_ok:
        issues.append(DraftSafetyIssue("recipient_mismatch", recipient_error or "Unsafe recipient.", "to_email"))

    context_ref = draft.get("context_packet_ref")
    context_email = _context_email(
        conn, project_id=project_id, context_ref=context_ref,
    )
    if context_ref and context_email is None:
        issues.append(DraftSafetyIssue(
            "invalid_context_reference",
            "Draft reply source is missing, outbound, or belongs to another project.",
            "context_packet_ref",
        ))
    context_sender = _normalize_email(context_email["sender_email"]) if context_email else ""
    if context_sender and context_sender != _normalize_email(to_email):
        issues.append(DraftSafetyIssue(
            "thread_recipient_mismatch",
            "Draft recipient does not match the referenced inbound email sender.",
            "to_email",
        ))

    if project_id:
        authority = _authoritative_project_text(conn, project_id)
        for label, value in _extract_identifiers(f"{subject}\n{body}"):
            if value.lower() not in authority:
                issues.append(DraftSafetyIssue(
                    "unsupported_identifier",
                    f"{label} '{value}' is not supported by project {project_id} evidence.",
                ))

    if not context_email and _PRIOR_MESSAGE_RE.search(body):
        issues.append(DraftSafetyIssue(
            "unsupported_prior_message_claim",
            "Draft refers to a prior client message or attachment without a valid inbound source.",
        ))

    from src.email.attachments import list_attachments
    attachment_rows = list_attachments(data_dir, draft.get("draft_id") or "")
    attachment_names = tuple(row["filename"] for row in attachment_rows)
    attachment_names_lower = {name.lower() for name in attachment_names}
    if _ATTACHED_RE.search(body) and not attachment_names:
        issues.append(DraftSafetyIssue(
            "attachment_claim_without_file",
            "Draft says a file is attached, but the attachment manifest is empty.",
        ))
    named_files = {match.lower() for match in _FILENAME_RE.findall(body)}
    missing_named = sorted(named_files - attachment_names_lower)
    if missing_named:
        issues.append(DraftSafetyIssue(
            "named_attachment_missing",
            f"Draft names attachment(s) not present in the manifest: {', '.join(missing_named)}.",
        ))
    if attachment_names and _MISSING_ATTACHMENT_RE.search(body):
        issues.append(DraftSafetyIssue(
            "attachment_state_contradiction",
            "Draft asks the recipient to resend or confirm an attachment that is already attached.",
        ))

    return DraftSafetyResult(
        ok=not issues,
        issues=issues,
        recipient_basis=recipient_basis,
        attachment_names=attachment_names,
    )
