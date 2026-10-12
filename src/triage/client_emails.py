"""Parse and filter projects.client_email — client contacts only, no vendor domains."""

from __future__ import annotations

import re
import sqlite3

from config.defaults import VENDOR_EMAIL_DOMAINS
from src.wiki.domain_index import get_wiki_domain_index

EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}", re.IGNORECASE)
_SPLIT_RE = re.compile(r"[,;\s]+")


def parse_client_email_addresses(raw_value: str | None) -> list[str]:
    """Extract deduplicated email addresses from a projects.client_email field."""
    if not raw_value:
        return []
    seen: set[str] = set()
    emails: list[str] = []
    for match in EMAIL_RE.findall(raw_value):
        email = match.strip().lower()
        if email and email not in seen:
            seen.add(email)
            emails.append(email)
    return emails


def email_domain(email: str) -> str:
    return email.rsplit("@", 1)[-1].strip().lower() if "@" in email else ""


def is_vendor_email_domain(domain: str, conn: sqlite3.Connection | None = None) -> bool:
    """Return True when *domain* is a shared vendor, not an end-client broker."""
    normalised = domain.strip().lower()
    if not normalised:
        return False
    if normalised in VENDOR_EMAIL_DOMAINS:
        return True
    if get_wiki_domain_index().is_vendor(normalised):
        return True
    if conn is not None:
        row = conn.execute(
            "SELECT org_type FROM organisations WHERE LOWER(domains) LIKE ? LIMIT 1",
            (f"%{normalised}%",),
        ).fetchone()
        if row and (row[0] or "").lower() == "vendor":
            return True
    return False


def is_vendor_email(email: str, conn: sqlite3.Connection | None = None) -> bool:
    return is_vendor_email_domain(email_domain(email), conn)


def filter_client_only_emails(
    raw_value: str | None,
    conn: sqlite3.Connection | None = None,
) -> list[str]:
    """Return client-side addresses only — strips vendor/counterparty contacts."""
    return [
        email
        for email in parse_client_email_addresses(raw_value)
        if not is_vendor_email(email, conn)
    ]


def format_client_email(emails: list[str]) -> str | None:
    """Join client emails for storage in projects.client_email."""
    if not emails:
        return None
    return "  ".join(emails)


def strip_vendor_emails_from_raw(
    raw_value: str | None,
    conn: sqlite3.Connection | None = None,
) -> str | None:
    """Return cleaned client_email text with vendor addresses removed."""
    return format_client_email(filter_client_only_emails(raw_value, conn))


def client_email_domains(
    raw_value: str | None,
    conn: sqlite3.Connection | None = None,
) -> set[str]:
    """Non-vendor domains referenced in a projects.client_email field."""
    return {
        email_domain(email)
        for email in filter_client_only_emails(raw_value, conn)
        if email_domain(email)
    }


def project_ids_for_sender_domain(
    conn: sqlite3.Connection,
    sender_email: str,
    *,
    active_only: bool = True,
) -> list[str]:
    """Find projects whose client_email includes the sender's non-vendor domain."""
    domain = email_domain(sender_email)
    if not domain or is_vendor_email_domain(domain, conn):
        return []

    status_clause = " AND project_status = 'Active'" if active_only else ""
    rows = conn.execute(
        f"SELECT project_id, client_email FROM projects WHERE client_email IS NOT NULL{status_clause}"
    ).fetchall()

    matches: list[str] = []
    for row in rows:
        domains = client_email_domains(row["client_email"], conn)
        if domain in domains:
            matches.append(row["project_id"])
    return matches