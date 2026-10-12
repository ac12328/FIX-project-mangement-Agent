"""
New Project Intake Flow (Blueprint Section 30 / Plan Step 3.6).
Two entry points: structured (web form) and free text (Telegram → LLM extraction).
No project committed without explicit operator confirmation.
"""

import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timezone

from src.models.project import Project
from src.models.enums import ProjectStatus, BallWith, Milestone
from src.repositories.project_repo import create_project, project_exists


@dataclass
class IntakeProposal:
    """A proposed new project from intake."""
    project_id: str
    client_name: str
    client_email: str | None = None
    market: str | None = None
    connection_type: str = "FIX 4.2"
    target_go_live: str | None = None
    broker_code: str | None = None
    notes: str | None = None
    source: str = "operator"  # operator | email | web_form | telegram | agent
    confidence: float = 1.0
    needs_confirmation: bool = True
    extraction_warnings: list[str] = field(default_factory=list)


def create_intake_from_structured(
    project_id: str,
    client_name: str,
    client_email: str | None = None,
    market: str | None = None,
    connection_type: str = "FIX 4.2",
    target_go_live: str | None = None,
    broker_code: str | None = None,
    notes: str | None = None,
    *,
    source: str = "web_form",
) -> IntakeProposal:
    """Create an intake proposal from structured data (web form / agent). No LLM."""
    return IntakeProposal(
        project_id=project_id,
        client_name=client_name,
        client_email=client_email,
        market=market,
        connection_type=connection_type,
        target_go_live=target_go_live,
        broker_code=broker_code,
        notes=notes,
        source=source,
        confidence=1.0,
        needs_confirmation=True,
    )


def create_intake_from_text(
    free_text: str,
    llm_extract_fn=None,
) -> IntakeProposal | None:
    """
    Create an intake proposal from free text via LLM extraction.
    Returns None if extraction fails.
    """
    if llm_extract_fn is None:
        return None

    try:
        extracted = llm_extract_fn(free_text)
    except Exception:
        return None

    if not extracted or not extracted.get("client_name"):
        return None

    warnings = []
    project_id = extracted.get("project_id")
    if not project_id:
        # Auto-generate project ID from client name
        safe_name = "".join(c for c in extracted["client_name"] if c.isalnum())[:10].upper()
        project_id = f"{safe_name}-001"
        warnings.append("Project ID auto-generated — please verify")

    return IntakeProposal(
        project_id=project_id,
        client_name=extracted["client_name"],
        client_email=extracted.get("client_email"),
        market=extracted.get("market"),
        connection_type=extracted.get("connection_type", "FIX 4.2"),
        target_go_live=extracted.get("target_go_live"),
        notes=extracted.get("notes"),
        source="telegram",
        confidence=extracted.get("confidence", 0.7),
        needs_confirmation=True,
        extraction_warnings=warnings,
    )


def confirm_intake(
    conn: sqlite3.Connection, proposal: IntakeProposal
) -> tuple[bool, str]:
    """
    Confirm and commit a new project intake.
    Returns (success, message).
    """
    if project_exists(conn, proposal.project_id):
        return False, f"Project {proposal.project_id} already exists"

    project = Project(
        project_id=proposal.project_id,
        client_name=proposal.client_name,
        client_email=proposal.client_email,
        market=proposal.market,
        project_status=ProjectStatus.ACTIVE,
        ball_with=BallWith.INTERNAL,
        current_milestone=Milestone.M1_NETWORK_SETUP,
        connection_type=proposal.connection_type,
        target_go_live=proposal.target_go_live,
        broker_code=proposal.broker_code,
        notes=proposal.notes,
    )

    create_project(conn, project)

    # Contact sync: create or link contact if email provided.
    # NOTE: primary_email is NOT auto-set — per Blueprint Section 15.5 Rule 3,
    # primary_email is set by operator only. System never auto-selects.
    if proposal.client_email:
        from src.repositories.contact_repo import find_contact_by_email, create_contact
        existing = find_contact_by_email(conn, proposal.client_email)
        if not existing:
            create_contact(
                conn,
                display_name=proposal.client_name,
                email_addresses=proposal.client_email,
                # primary_email intentionally omitted — operator must confirm
                contact_type="person",
            )

    return True, f"Project {proposal.project_id} created successfully"
