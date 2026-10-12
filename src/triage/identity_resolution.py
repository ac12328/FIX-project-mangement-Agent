"""
Identity Resolution Engine — deterministic, no LLM.
Resolves email sender to contact + organisation + project(s).
Prevents confusing an organization name with a project identifier.
"""

import sqlite3
from dataclasses import dataclass, field

from src.wiki.domain_index import get_wiki_domain_index


@dataclass
class IdentityMatch:
    """Result of identity resolution."""
    contact_id: str | None = None
    contact_name: str | None = None
    organisation_id: str | None = None
    organisation_name: str | None = None
    project_ids: list[str] = field(default_factory=list)
    confidence: float = 0.0
    resolution_method: str = "unresolved"
    needs_operator_check: bool = False


def resolve_sender(
    conn: sqlite3.Connection,
    sender_email: str,
    sender_name: str | None = None,
) -> IdentityMatch:
    """
    5-step deterministic identity resolution (per Blueprint Section 15).

    Step 1: Exact email match in contacts table (confidence 0.95)
    Step 2: Domain match to organisation (confidence 0.9)
    Step 3: Department detection (info@, support@, compliance@)
    Step 4: Cross-project detection (same domain on multiple projects)
    Step 5: If unresolved, flag for operator check

    NO LLM is used. This is purely deterministic.
    """
    from src.repositories.contact_repo import (
        find_contact_by_email,
        get_organisation_by_domain,
        get_contacts_by_organisation,
    )

    email = sender_email.lower().strip()
    domain = email.split("@")[1] if "@" in email else ""

    # Step 1: Exact email match (confidence 0.95 per Blueprint)
    # If sender is a known contact, use their identity directly — no operator check needed.
    contact = find_contact_by_email(conn, email)
    if contact:
        project_ids = _get_projects_for_domain(conn, email)
        return IdentityMatch(
            contact_id=contact["contact_id"],
            contact_name=contact["display_name"],
            organisation_id=contact.get("organisation_id"),
            organisation_name=_get_org_name(conn, contact.get("organisation_id")),
            project_ids=project_ids,
            confidence=0.95,
            resolution_method="exact_email_match",
        )

    # Step 2: Domain match to organisation (confidence 0.9 per Blueprint).
    # Only reached if sender not in contacts table.
    org = get_organisation_by_domain(conn, domain)
    if org:
        project_ids = _get_projects_for_domain(conn, email)
        return IdentityMatch(
            organisation_id=org["org_id"],
            organisation_name=org["org_name"],
            project_ids=project_ids,
            confidence=0.9,
            resolution_method="domain_match",
            needs_operator_check=True,
        )

    # Step 3: Department detection (info@, support@, compliance@)
    local_part = email.split("@")[0] if "@" in email else email
    department_prefixes = ["info", "support", "compliance", "admin", "helpdesk", "noreply"]
    if local_part in department_prefixes:
        return IdentityMatch(
            confidence=0.3,
            resolution_method="department_address",
            needs_operator_check=True,
        )

    # Step 4: Cross-project detection (same domain on multiple projects)
    project_ids = _get_projects_for_domain(conn, email)
    if project_ids:
        return IdentityMatch(
            project_ids=project_ids,
            confidence=0.5,
            resolution_method="domain_project_match",
            needs_operator_check=True,
        )

    # Step 5: Completely unknown — flag for operator
    return IdentityMatch(
        confidence=0.0,
        resolution_method="unresolved",
        needs_operator_check=True,
    )


def _get_projects_for_domain(
    conn: sqlite3.Connection, email: str
) -> list[str]:
    """Find active projects whose client_email shares the same client-side domain."""
    from src.triage.client_emails import email_domain, project_ids_for_sender_domain

    domain = email_domain(email)
    if not domain:
        return []
    if get_wiki_domain_index().is_internal(domain):
        return []
    return project_ids_for_sender_domain(conn, email, active_only=True)


def _get_org_name(conn: sqlite3.Connection, org_id: str | None) -> str | None:
    """Get organisation name by ID."""
    if org_id is None:
        return None
    row = conn.execute(
        "SELECT org_name FROM organisations WHERE org_id = ?", (org_id,)
    ).fetchone()
    return dict(row)["org_name"] if row else None
