"""
Operational Response Workflow (Blueprint Section 18).
Handles non-project emails: support, technical, vendor, internal.
Never writes project truth.
"""

import logging
import sqlite3
from collections.abc import Callable
from dataclasses import dataclass

from src.db.savepoint import savepoint

from src.triage.email_triage import ClassifiedEvent


@dataclass
class OperationalResult:
    """Result of operational email processing."""
    email_id: str
    action_taken: str  # auto_reply | draft_for_review | logged | forwarded | skipped
    draft_id: str | None = None
    reason: str = ""


# Patterns for auto-categorization
VENDOR_PATTERNS = ("vendor", "supplier", "invoice", "billing", "subscription", "renewal")
SUPPORT_PATTERNS = ("support", "help desk", "ticket", "case #", "incident")
INTERNAL_PATTERNS = ("internal", "reminder", "team", "meeting", "standup")


def handle_operational_email(
    conn: sqlite3.Connection,
    event: ClassifiedEvent,
    body: str,
    *,
    auto_reply_enabled: bool = False,
    llm_draft_fn=None,
    generated_draft: dict[str, str] | None = None,
    commit: bool = True,
    post_commit: list[Callable[[], None]] | None = None,
) -> OperationalResult:
    """
    Process an operational (non-project) email.
    This workflow NEVER writes project truth.

    Args:
        conn: Database connection.
        event: The classified email event.
        body: Email body text.
        auto_reply_enabled: If True, auto-replies to simple operational emails.
        llm_draft_fn: Optional callable to generate a draft response outside a write transaction.
        generated_draft: A response already generated before a caller-owned transaction.
        commit: Whether this command owns the final commit.
        post_commit: Required for a caller-owned generated draft so its events run
            only after the caller commits.

    Returns:
        OperationalResult with action taken.
    """
    subcategory = _categorize_operational(event.subject, body)

    def logged_result(reason: str | None = None) -> OperationalResult:
        _log_operational(conn, event.email_id, event.sender_email, event.subject,
                         subcategory, commit=commit)
        return OperationalResult(
            email_id=event.email_id,
            action_taken="logged",
            reason=reason or f"Operational email ({subcategory}) logged for operator review",
        )

    if subcategory == "system_notification":
        _log_operational(conn, event.email_id, event.sender_email, event.subject,
                         subcategory, commit=commit)
        return OperationalResult(
            email_id=event.email_id,
            action_taken="skipped",
            reason="System notification — no action needed",
        )

    if subcategory == "vendor" and auto_reply_enabled:
        return logged_result("Vendor email logged for reference")

    if (llm_draft_fn is not None or generated_draft is not None) and subcategory in ("support", "internal"):
        if llm_draft_fn is not None and generated_draft is not None:
            raise ValueError("Provide either llm_draft_fn or generated_draft")
        # The model must run before acquiring a write transaction. An already
        # borrowed transaction cannot safely release its writer lease here.
        if llm_draft_fn is not None and conn.in_transaction:
            raise ValueError("Generate an operational draft outside an active transaction")
        if not commit and post_commit is None:
            raise ValueError("A caller-owned operational draft requires post_commit effects")
        try:
            draft_result = (llm_draft_fn(event.sender_email, event.subject, body)
                            if llm_draft_fn is not None else generated_draft)
            draft_subject = draft_result.get("subject", event.subject)
            draft_body = draft_result.get("body", "")
        except Exception:
            return logged_result()

        effects: list[Callable[[], None]] = post_commit if post_commit is not None else []
        effect_start = len(effects)
        try:
            with savepoint(conn, ensure_transaction=True):
                _log_operational(conn, event.email_id, event.sender_email,
                                 event.subject, subcategory, commit=False)
                draft_id = _save_operational_draft(
                    conn, event.sender_email,
                    draft_subject, draft_body, post_commit=effects,
                )
                from src.telegram.draft_card_outbox import enqueue_draft_card

                enqueue_draft_card(
                    conn, draft_id=draft_id, to_email=event.sender_email,
                    subject=draft_subject,
                )
        except Exception:
            del effects[effect_start:]
            return logged_result()

        if commit:
            conn.commit()
            for effect in effects[effect_start:]:
                try:
                    effect()
                except Exception:
                    logging.getLogger(__name__).exception(
                        "Committed operational draft event failed for %s", draft_id,
                    )
        return OperationalResult(
            email_id=event.email_id,
            action_taken="draft_for_review",
            draft_id=draft_id,
            reason=f"Draft generated for {subcategory} email",
        )

    return logged_result()


def _categorize_operational(subject: str, body: str) -> str:
    """Categorize operational email into subcategory."""
    combined = (subject + " " + body[:300]).lower()

    if any(p in combined for p in VENDOR_PATTERNS):
        return "vendor"
    if any(p in combined for p in SUPPORT_PATTERNS):
        return "support"
    if any(p in combined for p in INTERNAL_PATTERNS):
        return "internal"

    # Check if it's a system notification
    from src.triage.email_triage import OPERATIONAL_PATTERNS
    sender_lower = ""  # We don't have sender here, but subject might hint
    if any(kw in combined for kw in ("automated", "notification", "alert", "system")):
        return "system_notification"

    return "general"


def _log_operational(
    conn: sqlite3.Connection,
    email_id: str,
    sender: str,
    subject: str,
    subcategory: str,
    *,
    commit: bool = True,
) -> None:
    """Log an operational email for audit trail."""
    from datetime import datetime, timezone
    now = datetime.now(timezone.utc).isoformat()
    conn.execute(
        """INSERT OR IGNORE INTO processed_emails
           (email_id, project_id, processed_at, received_at, result)
           VALUES (?, NULL, ?, ?, ?)""",
        (email_id, now, now, f"operational:{subcategory}"),
    )
    if commit:
        conn.commit()


def _save_operational_draft(
    conn: sqlite3.Connection,
    to_email: str,
    subject: str,
    body: str,
    *,
    post_commit: list[Callable[[], None]],
) -> str:
    """Save an operational draft in its caller-owned command transaction."""
    from src.email.draft_session import create_draft
    return create_draft(
        conn, project_id=None, task_type="operational_reply",
        subject=subject, body=body, to_email=to_email,
        commit=False, post_commit=post_commit,
    )
