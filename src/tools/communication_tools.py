"""Tool contract and handler for universal ad-hoc email drafting."""

from __future__ import annotations

import sqlite3
from typing import Any

from config import defaults
from src.communications.compose_service import compose_email_draft
from src.tools.contracts import ToolContract


CREATE_EMAIL_DRAFT_TOOL = ToolContract(
    name="create_email_draft",
    description=(
        "Create a reviewable ad-hoc email draft without requiring a project. "
        "Use an explicit to_email, contact_id, or unambiguous contact_name. "
        "This creates a draft only and never sends the email."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "context": {"type": "string", "description": "What the email should communicate"},
            "to_email": {"type": "string"},
            "contact_id": {"type": "string"},
            "contact_name": {"type": "string"},
            "subject": {"type": "string"},
            "body": {"type": "string"},
            "cc": {"type": "string"},
            "purpose": {"type": "string"},
            "related_project_id": {"type": "string"},
            "scheduled_send_at": {"type": "string"},
        },
        "required": ["context"],
    },
    output_schema={"type": "object"},
    allowed_task_types=["*"],
    allowed_contexts=["chat"],
    side_effect_type="write",
    timeout_seconds=30,
    audit_required=True,
)


def chat_create_email_draft(conn: sqlite3.Connection, args: dict) -> dict:
    if not (
        defaults.PERSONAL_ASSISTANT_ENABLED
        and defaults.PERSONAL_ASSISTANT_UNIVERSAL_EMAIL_ENABLED
    ):
        return {"error": "Universal email drafting is disabled"}
    try:
        result = compose_email_draft(
            conn,
            context=(args.get("context") or "").strip(),
            to_email=args.get("to_email"),
            contact_id=args.get("contact_id"),
            contact_name=args.get("contact_name"),
            subject=args.get("subject"),
            body=args.get("body"),
            cc=args.get("cc"),
            purpose=(args.get("purpose") or "personal_email").strip(),
            related_project_id=args.get("related_project_id"),
            scheduled_send_at=args.get("scheduled_send_at"),
        )
    except ValueError as exc:
        return {"error": str(exc)}
    return {
        "status": "created",
        "draft_id": result.draft_id,
        "to_email": result.to_email,
        "subject": result.subject,
        "contact_id": result.contact_id,
        "message": "Draft created for review. The email has not been sent.",
    }


COMMUNICATION_TOOL_HANDLERS: dict[str, Any] = {
    "create_email_draft": chat_create_email_draft,
}
