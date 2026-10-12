"""
Multi-Project Same-Client Handling (Plan Step 3.11, Blueprint Section 27).
Email routing priority: thread match → subject keyword → Ambiguous Routing Card.
Response pattern: project-override first, then global.
"""

import sqlite3
from dataclasses import dataclass, field


@dataclass
class RoutingResult:
    """Result of multi-project email routing."""
    project_id: str | None = None
    confidence: float = 0.0
    method: str = "unresolved"  # thread | keyword | single | ambiguous
    candidates: list[str] = field(default_factory=list)
    needs_operator: bool = False


def route_email_to_project(
    conn: sqlite3.Connection,
    sender_email: str,
    subject: str,
    *,
    in_reply_to: str | None = None,
    references: str | None = None,
) -> RoutingResult:
    """
    Route an email to a project when the sender has multiple active projects.

    Priority:
    1. Thread match (In-Reply-To / References) → auto-route (confidence 0.95)
    2. Subject keyword match → auto-route if single match (confidence 0.80)
    3. Single project → auto-route (confidence 0.90)
    4. Multiple candidates → Ambiguous Routing Card (needs_operator=True)
    """
    # Step 1: Thread match
    if in_reply_to or references:
        from src.email.threading import find_thread_project
        thread_project = find_thread_project(conn, in_reply_to=in_reply_to, references=references)
        if thread_project:
            return RoutingResult(
                project_id=thread_project,
                confidence=0.95,
                method="thread",
            )

    # Step 2: Find all projects for this sender's domain
    domain = sender_email.split("@")[1].lower() if "@" in sender_email else ""
    candidates = _find_projects_for_domain(conn, domain)

    if not candidates:
        # Try direct contact match
        candidates = _find_projects_for_email(conn, sender_email)

    if not candidates:
        return RoutingResult(method="unresolved")

    # Step 3: Single project → auto-route
    if len(candidates) == 1:
        return RoutingResult(
            project_id=candidates[0],
            confidence=0.90,
            method="single",
            candidates=candidates,
        )

    # Step 4: Subject keyword match
    keyword_matches = _match_by_subject(conn, candidates, subject)
    if len(keyword_matches) == 1:
        return RoutingResult(
            project_id=keyword_matches[0],
            confidence=0.80,
            method="keyword",
            candidates=candidates,
        )

    # Step 5: Ambiguous — needs operator
    return RoutingResult(
        confidence=0.0,
        method="ambiguous",
        candidates=candidates,
        needs_operator=True,
    )


def _find_projects_for_domain(conn: sqlite3.Connection, domain: str) -> list[str]:
    """Find active projects whose client_email includes this client-side domain."""
    from src.triage.client_emails import is_vendor_email_domain, project_ids_for_sender_domain

    if not domain or is_vendor_email_domain(domain, conn):
        return []
    return project_ids_for_sender_domain(
        conn,
        f"routing-placeholder@{domain}",
        active_only=True,
    )


def _find_projects_for_email(conn: sqlite3.Connection, email: str) -> list[str]:
    """Find active projects by exact client email."""
    rows = conn.execute(
        """SELECT project_id FROM projects
           WHERE project_status = 'Active'
           AND LOWER(client_email) = LOWER(?)""",
        (email,),
    ).fetchall()
    return [r["project_id"] for r in rows]


def _match_by_subject(conn: sqlite3.Connection, candidates: list[str], subject: str) -> list[str]:
    """Find which candidate projects are mentioned in the subject."""
    subject_lower = subject.lower()
    matches = []
    for pid in candidates:
        # Check if project ID appears in subject
        if pid.lower() in subject_lower:
            matches.append(pid)
            continue
        # Check if client name appears in subject
        row = conn.execute(
            "SELECT client_name FROM projects WHERE project_id = ?",
            (pid,),
        ).fetchone()
        if row and row["client_name"] and row["client_name"].lower() in subject_lower:
            matches.append(pid)
    return matches
