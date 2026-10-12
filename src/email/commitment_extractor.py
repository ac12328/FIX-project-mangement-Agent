"""
Extract commitments from inbound client replies and auto-create follow-ups.

When a client email contains a commitment (e.g. "we will send the config by Friday"),
this module detects it via LLM and creates an internal follow-up so the operator
doesn't have to track it manually.
"""

import logging
import sqlite3
from datetime import datetime, timezone

from config.defaults import EMAIL_BODY_SNIPPET_CHARS
from src.llm.client import llm_call_structured
from src.utils.calendar import next_business_day

from src.utils.prompt_loader import load_prompt

logger = logging.getLogger(__name__)

_COMMITMENT_SCHEMA = {
    "type": "object",
    "properties": {
        "has_commitment": {"type": "boolean"},
        "commitment_text": {"type": ["string", "null"]},
        "due_date": {"type": ["string", "null"]},
    },
    "required": ["has_commitment"],
}

def extract_commitment(
    project_id: str,
    body_text: str,
) -> dict | None:
    """
    Call LLM to detect whether *body_text* contains a client commitment.

    Returns a dict with keys ``has_commitment``, ``commitment_text``,
    ``due_date`` — or ``None`` on any exception (graceful fallback).
    """
    try:
        truncated = (body_text or "")[:EMAIL_BODY_SNIPPET_CHARS]
        messages = [
            {"role": "system", "content": load_prompt("routing/commitment_extractor.md")},
            {
                "role": "user",
                "content": (
                    f"Project: {project_id}\n\n"
                    f"{truncated}"
                ),
            },
        ]
        result = llm_call_structured(
            "commitment_extraction", messages, _COMMITMENT_SCHEMA
        )
        return result
    except Exception:
        logger.exception("Commitment extraction failed for project %s", project_id)
        return None


def create_followup_from_commitment(
    conn: sqlite3.Connection,
    project_id: str,
    commitment: dict,
    *,
    commit: bool = True,
) -> str | None:
    """
    Create an internal follow-up from a detected commitment.

    Missing or invalid commitment dates remain NULL. A configurable check-in
    is scheduled separately as reminder_due_at, never as a client promise.
    """
    from src.portfolio.followup_tracker import create_followup

    commitment_text = commitment.get("commitment_text") or "Client commitment"
    due_date = commitment.get("due_date")

    # Validate due_date format — fall through to default if LLM returned garbage
    if due_date:
        try:
            datetime.strptime(due_date, "%Y-%m-%d")
        except (ValueError, TypeError):
            logger.warning("Invalid due_date format from LLM: %r — using default", due_date)
            due_date = None

    reminder_due_at = None
    if not due_date:
        from config.settings import CONFIG
        followup_days = CONFIG.thresholds.commitment_followup_days
        dt = datetime.now(timezone.utc)
        for _ in range(followup_days):
            dt = next_business_day(dt)
        reminder_due_at = dt.strftime("%Y-%m-%d")

    followup_id = create_followup(
        conn,
        project_id=project_id,
        description=f"[Auto] {commitment_text}",
        due_date=due_date,
        reminder_due_at=reminder_due_at,
        created_by="auto_resolved",
        commit=commit,
    )
    return followup_id
