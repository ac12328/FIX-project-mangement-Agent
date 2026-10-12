"""
LLM-assisted Stage 3 email triage classifier.
Extracted from email_triage.py to keep file sizes ≤ 500 lines.
"""

from __future__ import annotations

import logging
import re
import sqlite3

from src.triage.email_triage import ClassifiedEvent
from src.wiki.domain_index import get_wiki_domain_index

logger = logging.getLogger(__name__)

# Pattern for verified project ID tags in email subjects
_SUBJECT_TAG_RE = re.compile(r"\[([A-Z]{2}\d{4})]")


def _get_thread_history(conn: sqlite3.Connection, sender_email: str) -> list[dict]:
    """
    Fetch recent email thread history for a sender from processed_emails.
    Returns up to 5 most recent emails from the same sender, newest first.
    """
    rows = conn.execute(
        """SELECT email_id, subject, processed_at, result, thread_fp
           FROM processed_emails
           WHERE sender_email = ? AND direction = 'inbound'
           ORDER BY processed_at DESC
           LIMIT 5""",
        (sender_email.lower(),),
    ).fetchall()
    return [dict(r) for r in rows]


def _get_contact_hint(conn: sqlite3.Connection, sender_email: str) -> dict | None:
    """
    Get contact hints for a sender: known contact name, organisation, role.
    Returns None if sender is unknown.
    """
    from src.triage.identity_resolution import resolve_sender

    identity = resolve_sender(conn, sender_email)
    if not identity.contact_id:
        return None
    row = conn.execute(
        """SELECT c.display_name, c.role, o.org_name
           FROM contacts c
           LEFT JOIN organisations o ON c.organisation_id = o.org_id
           WHERE c.contact_id = ?""",
        (identity.contact_id,),
    ).fetchone()
    if not row:
        return None
    d = dict(row)
    return {
        "contact_name": d.get("display_name"),
        "role": d.get("role"),
        "organisation": d.get("org_name"),
        "confidence": identity.confidence,
    }


def _validate_project_assignment(
    llm_project_id: str | None,
    sender_email: str,
    subject: str,
    projects: list[dict],
    contact_hint: dict | None,
    *,
    body: str | None = None,
) -> str | None:
    """
    Validate that the LLM's project_id assignment is plausible.
    Returns the project_id if valid, None if the assignment looks hallucinated.

    Checks (any passing = valid):
    1. project_id exists in active projects list
    2. Subject contains [PROJECT_ID] tag matching the assigned project
    3. Sender domain matches the project's client_email domain
    4. Contact hints confirm the sender belongs to that project

    Hard block: ops-only subjects (stock code conversion, SBL, …) must not
    attach to onboarding projects via contact multiplex alone.
    See wiki/pattern/email_vendor_multiplex_mislink.md
    """
    if not llm_project_id:
        return None
    valid_ids = {p["project_id"] for p in projects}
    if llm_project_id not in valid_ids:
        logger.warning("LLM returned non-existent project_id %r — clearing", llm_project_id)
        return None

    from src.email.ops_mail_guard import (
        has_project_subject_tag,
        should_block_project_truth_from_email,
        should_block_reopen_completed_without_tag,
    )

    if should_block_project_truth_from_email(
        subject, project_id=llm_project_id, body=body,
    ):
        logger.info(
            "Blocked project assignment %r for ops-only email (multiplex guard). "
            "Subject: %r",
            llm_project_id,
            (subject or "")[:80],
        )
        return None

    # Do not attach untagged mail to a Completed/Archived project
    proj_row = next((p for p in projects if p["project_id"] == llm_project_id), None)
    # projects list is often Active-only; also check status if present
    cur_status = (proj_row or {}).get("project_status")
    if cur_status and should_block_reopen_completed_without_tag(
        subject, project_id=llm_project_id, current_status=cur_status,
    ):
        logger.info(
            "Blocked assignment to terminal project %r without subject tag. Subject: %r",
            llm_project_id,
            (subject or "")[:80],
        )
        return None

    # Check 1: subject contains [PROJECT_ID] tag
    subject_tags = _SUBJECT_TAG_RE.findall(subject)
    if llm_project_id in subject_tags:
        return llm_project_id

    # Check 2: sender domain matches project's client-side email domain (not vendor)
    from src.triage.client_emails import client_email_domains, email_domain, is_vendor_email_domain

    sender_domain = email_domain(sender_email)
    if sender_domain and not is_vendor_email_domain(sender_domain):
        project = next((p for p in projects if p["project_id"] == llm_project_id), None)
        if project and project.get("client_email"):
            if sender_domain in client_email_domains(project["client_email"]):
                # Still require FIX context OR project tag for domain match alone
                # when subject is bulk/ops noise — contact path remains for real FIX
                if has_project_subject_tag(subject, llm_project_id):
                    return llm_project_id
                from src.email.ops_mail_guard import has_fix_onboarding_context
                if has_fix_onboarding_context(subject, body):
                    return llm_project_id
                # Domain match without FIX/onboarding signal — weak; fall through
                # to contact check only if contact exists

    # Check 3: contact hints confirm sender is a known contact
    # Vendor/client contacts serve many threads — require FIX context or tag
    if contact_hint is not None:
        from src.email.ops_mail_guard import has_fix_onboarding_context

        if has_project_subject_tag(subject, llm_project_id) or has_fix_onboarding_context(
            subject, body
        ):
            return llm_project_id
        logger.info(
            "Contact match alone insufficient for project %r (no tag/FIX context). "
            "Subject: %r",
            llm_project_id,
            (subject or "")[:80],
        )
        return None

    logger.warning(
        "LLM assigned project_id %r but no corroborating signal "
        "(no subject tag, no domain match, no contact). Subject: %r, sender: %s",
        llm_project_id, subject[:80], sender_email,
    )
    return None


def run_stage3_llm(
    conn: sqlite3.Connection,
    email_id: str,
    sender_email: str,
    subject: str,
    body: str,
    llm_classify_fn,
) -> ClassifiedEvent:
    """Stage 3: LLM-assisted classification with structured context."""
    from src.llm.client import LLMParseFailure

    rows = conn.execute(
        "SELECT project_id, client_name, client_email FROM projects WHERE project_status = 'Active'"
    ).fetchall()
    projects = [dict(r) for r in rows]

    thread_history = _get_thread_history(conn, sender_email)
    contact_hint = _get_contact_hint(conn, sender_email)

    try:
        llm_result = llm_classify_fn(
            sender_email, subject, body, projects, thread_history, contact_hint
        )
        confidence = llm_result.get("confidence", 0.0)
        classification = llm_result.get("classification_type", "ambiguous")

        # Accept new support types from LLM — route directly
        if classification in ("support_issue", "project_support") and confidence >= 0.75:
            support_pid = llm_result.get("project_id")
            from src.email.ops_mail_guard import should_block_project_truth_from_email

            if support_pid and should_block_project_truth_from_email(
                subject, project_id=support_pid, body=body,
            ):
                # Ops mail (e.g. stock-code conversion) must not stick to onboarding project
                support_pid = None
                classification = "support_issue"
            return ClassifiedEvent(
                email_id=email_id,
                sender_email=sender_email,
                subject=subject,
                classification_type=classification,
                project_id=support_pid,
                confidence=confidence,
                triage_stage_reached=3,
                reason=llm_result.get("reason", "LLM support classification"),
                should_notify_operator=True,
                notification_reason=f"llm_{classification}",
            )

        if confidence >= 0.75:
            # Validate project_id — reject hallucinated assignments
            raw_project_id = llm_result.get("project_id")
            validated_project_id = _validate_project_assignment(
                raw_project_id, sender_email, subject, projects, contact_hint,
                body=body,
            )
            if classification == "project_update" and raw_project_id and not validated_project_id:
                # LLM assigned a project but validation failed — downgrade to ambiguous
                return ClassifiedEvent(
                    email_id=email_id,
                    sender_email=sender_email,
                    subject=subject,
                    classification_type="ambiguous",
                    confidence=confidence,
                    triage_stage_reached=3,
                    reason=f"LLM assigned project {raw_project_id} but no corroborating signal (subject tag, domain match, or contact)",
                    requires_operator_triage=True,
                    should_notify_operator=True,
                    notification_reason="llm_project_assignment_unverified",
                )

            # If the sender is completely unknown (no contact in registry, no prior emails)
            # and the LLM is assigning this as a project_update, always require operator
            # review — the LLM is guessing without any confirmed signal.
            # Exception: internal domain senders (per WikiDomainIndex) are always trusted.
            unknown_sender = contact_hint is None and not thread_history
            if unknown_sender:
                domain = sender_email.rsplit("@", 1)[-1].lower() if "@" in sender_email else ""
                if get_wiki_domain_index().is_internal(domain):
                    unknown_sender = False
            if classification == "project_update" and unknown_sender:
                return ClassifiedEvent(
                    email_id=email_id,
                    sender_email=sender_email,
                    subject=subject,
                    classification_type="project_update",
                    project_id=validated_project_id,
                    confidence=confidence,
                    triage_stage_reached=3,
                    reason=llm_result.get("reason", "LLM classification — unknown sender, operator review required"),
                    should_notify_operator=True,
                    requires_operator_triage=True,
                    notification_reason="llm_match_unknown_sender",
                )
            return ClassifiedEvent(
                email_id=email_id,
                sender_email=sender_email,
                subject=subject,
                classification_type=classification,
                project_id=validated_project_id,
                confidence=confidence,
                triage_stage_reached=3,
                reason=llm_result.get("reason", "LLM classification"),
                should_notify_operator=(classification == "new_inquiry"),
                notification_reason=("new_inquiry" if classification == "new_inquiry" else None),
                suppressed_reason=("project_update_auto_processed" if classification == "project_update" else None),
            )
        return ClassifiedEvent(
            email_id=email_id,
            sender_email=sender_email,
            subject=subject,
            classification_type="ambiguous",
            confidence=confidence,
            triage_stage_reached=3,
            reason=f"LLM confidence too low ({confidence:.2f})",
            requires_operator_triage=True,
            should_notify_operator=True,
            notification_reason="llm_low_confidence",
        )
    except LLMParseFailure as e:
        short_err = str(e).splitlines()[0][:120]
        return ClassifiedEvent(
            email_id=email_id,
            sender_email=sender_email,
            subject=subject,
            classification_type="llm_failed",
            triage_stage_reached=3,
            reason=f"LLM classification unavailable ({short_err})",
            requires_operator_triage=True,
            should_notify_operator=True,
            notification_reason="llm_classification_failed",
            llm_failed=True,
        )
    except Exception as e:
        short_err = str(e).splitlines()[0][:120]
        return ClassifiedEvent(
            email_id=email_id,
            sender_email=sender_email,
            subject=subject,
            classification_type="ambiguous",
            triage_stage_reached=3,
            reason=f"LLM classification unavailable ({short_err})",
            requires_operator_triage=True,
            should_notify_operator=True,
            notification_reason="llm_classification_error",
            llm_failed=True,
        )
