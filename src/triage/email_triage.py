"""
3-Stage Email Triage Pipeline.
Stage 1: Deterministic (thread_id / in_reply_to match)
Stage 2: Heuristic (domain + FIX keyword check)
Stage 3: LLM-assisted (only for ambiguous emails)
"""

import sqlite3
from dataclasses import dataclass

from src.wiki.domain_index import get_wiki_domain_index


@dataclass
class ClassifiedEvent:
    """Output of the triage pipeline (per Blueprint Section 17.2)."""
    email_id: str
    sender_email: str
    subject: str
    classification_type: str  # project_update | operational | new_inquiry | internal | ambiguous | llm_failed | support_issue | project_support
    project_id: str | None = None
    confidence: float = 0.0
    triage_stage_reached: int = 0  # 1, 2, or 3
    routing_hint: str | None = None
    reason: str = ""
    requires_operator_triage: bool = False
    should_notify_operator: bool = False
    notification_reason: str | None = None
    suppressed_reason: str | None = None
    llm_failed: bool = False
    # Per Blueprint Section 17.2 schema fields:
    event_id: str | None = None
    source: str | None = None  # e.g. "email", "telegram"
    sender_contact_id: str | None = None
    organisation_id: str | None = None
    # Support ticket fields (WP-03):
    ticket_id: str | None = None
    counterparty: str | None = None


from src.triage.email_signals import (
    FIX_KEYWORDS,
    INTERNAL_OPERATIONAL_SENDER_PATTERNS,
    OPERATIONAL_BODY_KEYWORDS,
    OPERATIONAL_PATTERNS,
    OPERATIONAL_SUBJECTS,
    SUBJECT_PROJECT_FIX_HINTS,
)


def triage_email(
    conn: sqlite3.Connection,
    email_id: str,
    sender_email: str,
    subject: str,
    body: str,
    thread_id: str | None = None,
    in_reply_to: str | None = None,
    llm_classify_fn=None,
) -> ClassifiedEvent:
    """
    Run the 3-stage triage pipeline.

    Args:
        conn: Database connection.
        email_id: Unique email identifier.
        sender_email: Sender's email address.
        subject: Email subject line.
        body: Email body text.
        thread_id: Optional thread ID from email headers.
        in_reply_to: Optional In-Reply-To header value.
        llm_classify_fn: Optional callable for Stage 3 LLM classification.
            Signature: (sender, subject, body, project_ids) -> dict with
            {classification_type, project_id, confidence, reason}

    Returns:
        ClassifiedEvent with classification result.
    """
    # Stage 1: Deterministic — thread/contact match
    result = _stage1_deterministic(conn, email_id, sender_email, subject, body, thread_id, in_reply_to)
    if result is not None:
        return result

    # Stage 2: Domain & signal heuristics
    result = _stage2_heuristic(conn, email_id, sender_email, subject, body)
    if result is not None:
        return result

    # Stage 3: LLM-assisted (if classifier provided)
    if llm_classify_fn is not None:
        from src.triage.llm_classifier import run_stage3_llm
        return run_stage3_llm(conn, email_id, sender_email, subject, body, llm_classify_fn)

    # No LLM available — push to operator
    return ClassifiedEvent(
        email_id=email_id,
        sender_email=sender_email,
        subject=subject,
        classification_type="ambiguous",
        triage_stage_reached=2,
        reason="No LLM classifier available, needs manual triage",
        requires_operator_triage=True,
        should_notify_operator=True,
        notification_reason="no_classifier_available",
    )


def _sender_domain(sender_email: str) -> str:
    email_lower = sender_email.lower()
    return email_lower.split("@")[-1] if "@" in email_lower else ""


def _stage1_deterministic(
    conn: sqlite3.Connection,
    email_id: str,
    sender_email: str,
    subject: str,
    body: str,
    thread_id: str | None,
    in_reply_to: str | None,
) -> ClassifiedEvent | None:
    """Stage 1: Check thread_id and in_reply_to for existing conversations."""
    subject_project_id = _extract_active_project_id_from_subject(conn, subject)
    if subject_project_id and _has_subject_project_fix_signal(subject, body):
        pm_forward = get_wiki_domain_index().is_internal(_sender_domain(sender_email))
        return ClassifiedEvent(
            email_id=email_id,
            sender_email=sender_email,
            subject=subject,
            classification_type="project_update",
            project_id=subject_project_id,
            confidence=0.95,
            triage_stage_reached=1,
            reason=(
                "subject project ID match with FIX signal"
                + (" (PM forward)" if pm_forward else "")
            ),
            routing_hint="pm_forward" if pm_forward else None,
            suppressed_reason="project_update_auto_processed",
        )

    # Check if thread_id matches a known project event
    if thread_id:
        row = conn.execute(
            """SELECT project_id FROM project_events
               WHERE email_id = ? OR metadata_json LIKE ?
               ORDER BY created_at DESC LIMIT 1""",
            (thread_id, f'%"thread_id":"{thread_id}"%'),
        ).fetchone()
        if row:
            return ClassifiedEvent(
                email_id=email_id,
                sender_email=sender_email,
                subject=subject,
                classification_type="project_update",
                project_id=dict(row)["project_id"],
                confidence=0.95,
                triage_stage_reached=1,
                reason="thread_id match",
                suppressed_reason="project_update_auto_processed",
            )

    # Check if in_reply_to matches an email WE sent (outbound only).
    # Inheriting project from another inbound email is wrong — those were sent
    # by third parties and may belong to different projects or conversations.
    if in_reply_to:
        row = conn.execute(
            """SELECT project_id FROM processed_emails
               WHERE email_id = ? AND direction = 'outbound'""",
            (in_reply_to,),
        ).fetchone()
        if row:
            return ClassifiedEvent(
                email_id=email_id,
                sender_email=sender_email,
                subject=subject,
                classification_type="project_update",
                project_id=dict(row)["project_id"],
                confidence=0.90,
                triage_stage_reached=1,
                reason="in_reply_to match (outbound)",
                suppressed_reason="project_update_auto_processed",
            )

    # Check sender contact → exact project match
    from src.triage.identity_resolution import resolve_sender
    identity = resolve_sender(conn, sender_email)
    if identity.confidence >= 0.9 and len(identity.project_ids) == 1 and has_fix_signal(subject, body):
        return ClassifiedEvent(
            email_id=email_id,
            sender_email=sender_email,
            subject=subject,
            classification_type="project_update",
            project_id=identity.project_ids[0],
            confidence=identity.confidence,
            triage_stage_reached=1,
            reason=f"exact contact match via {identity.resolution_method} with FIX signal",
            suppressed_reason="project_update_auto_processed",
        )

    return None


def _stage2_heuristic(
    conn: sqlite3.Connection,
    email_id: str,
    sender_email: str,
    subject: str,
    body: str,
) -> ClassifiedEvent | None:
    """Stage 2: Domain and keyword heuristics."""
    email_lower = sender_email.lower()
    subject_project_id = _extract_active_project_id_from_subject(conn, subject)

    # Internal company sender — suppress but still link to project if subject identifies one
    sender_domain = email_lower.split("@")[-1] if "@" in email_lower else ""
    if get_wiki_domain_index().is_internal(sender_domain):
        return ClassifiedEvent(
            email_id=email_id,
            sender_email=sender_email,
            subject=subject,
            classification_type="internal",
            confidence=1.0,
            triage_stage_reached=2,
            reason=f"internal company domain: {sender_domain}",
            suppressed_reason="internal_company_domain",
            project_id=subject_project_id,
        )

    # Check for known operational/system sender patterns
    for pattern in OPERATIONAL_PATTERNS:
        if pattern in email_lower:
            return ClassifiedEvent(
                email_id=email_id,
                sender_email=sender_email,
                subject=subject,
                classification_type="operational",
                confidence=0.80,
                triage_stage_reached=2,
                reason=f"operational sender pattern: {pattern}",
                suppressed_reason="operational_sender_pattern",
            )

    for pattern in INTERNAL_OPERATIONAL_SENDER_PATTERNS:
        if pattern in email_lower:
            return ClassifiedEvent(
                email_id=email_id,
                sender_email=sender_email,
                subject=subject,
                classification_type="operational",
                confidence=0.85,
                triage_stage_reached=2,
                reason=f"internal operational sender pattern: {pattern}",
                suppressed_reason="internal_operational_sender",
            )

    # Check for operational subject patterns (remittances, settlement, system notifications)
    subject_lower = subject.lower()
    for pattern in OPERATIONAL_SUBJECTS:
        if pattern in subject_lower:
            return ClassifiedEvent(
                email_id=email_id,
                sender_email=sender_email,
                subject=subject,
                classification_type="operational",
                confidence=0.80,
                triage_stage_reached=2,
                reason=f"operational subject pattern: {pattern}",
                suppressed_reason="operational_subject_pattern",
            )

    # Check for FIX keywords in subject + body
    combined = (subject + " " + body[:500]).lower()
    for pattern in OPERATIONAL_BODY_KEYWORDS:
        if pattern in combined:
            return ClassifiedEvent(
                email_id=email_id,
                sender_email=sender_email,
                subject=subject,
                classification_type="operational",
                confidence=0.75,
                triage_stage_reached=2,
                reason=f"operational body pattern: {pattern}",
                suppressed_reason="operational_body_pattern",
            )

    # Project identity guard — before support signal check.
    # A known project contact sending an email with a FIX signal is a project
    # matter — NOT a standalone support issue — regardless of error keywords in body.
    # Single project → route directly. Multiple projects → ambiguous (operator decides).
    # Either way, must NOT become a support ticket.
    if has_fix_signal(subject, body):
        from src.triage.identity_resolution import resolve_sender
        _identity = resolve_sender(conn, sender_email)
        if _identity.confidence >= 0.9 and len(_identity.project_ids) == 1:
            return ClassifiedEvent(
                email_id=email_id,
                sender_email=sender_email,
                subject=subject,
                classification_type="project_update",
                project_id=_identity.project_ids[0],
                confidence=_identity.confidence,
                triage_stage_reached=2,
                reason=f"known project contact ({_identity.resolution_method}) with FIX signal — project_update takes priority over support signals",
                suppressed_reason="project_update_auto_processed",
            )
        if _identity.confidence >= 0.9 and len(_identity.project_ids) > 1:
            # Multi-project contact — cannot auto-route, but must not become a support ticket.
            # Operator must decide which project this belongs to.
            return ClassifiedEvent(
                email_id=email_id,
                sender_email=sender_email,
                subject=subject,
                classification_type="ambiguous",
                confidence=0.5,
                triage_stage_reached=2,
                reason=f"known contact ({_identity.resolution_method}) has {len(_identity.project_ids)} projects with FIX signal — operator must assign",
                requires_operator_triage=True,
                should_notify_operator=True,
                notification_reason="multi_project_ambiguous",
            )

    # Support signal check — before FIX keyword routing.
    # Only fires for senders NOT already matched as operational above.
    from src.triage.support_signals import detect_support_type, count_support_signals
    support_type = detect_support_type(conn, sender_email, subject, body)
    if support_type is not None:
        sig_count = count_support_signals(subject, body)
        return ClassifiedEvent(
            email_id=email_id,
            sender_email=sender_email,
            subject=subject,
            classification_type=support_type,
            confidence=min(0.90, 0.60 + sig_count * 0.05),
            triage_stage_reached=2,
            reason=f"support signals detected ({sig_count} hits) → {support_type}",
            should_notify_operator=True,
            notification_reason=f"support_{support_type}",
        )

    keyword_hits = sum(1 for kw in FIX_KEYWORDS if kw in combined)

    if keyword_hits >= 3:
        # Strong FIX signal — try domain match to narrow project
        from src.triage.identity_resolution import resolve_sender
        identity = resolve_sender(conn, sender_email)
        if identity.project_ids:
            if len(identity.project_ids) == 1:
                return ClassifiedEvent(
                    email_id=email_id,
                    sender_email=sender_email,
                    subject=subject,
                    classification_type="project_update",
                    project_id=identity.project_ids[0],
                    confidence=0.75,
                    triage_stage_reached=2,
                    reason=f"FIX keywords ({keyword_hits} hits) + domain project match",
                    suppressed_reason="project_update_auto_processed",
                )
            # Multiple projects — try multi-project routing
            from src.triage.multi_project import route_email_to_project
            routing = route_email_to_project(
                conn, sender_email, subject,
            )
            if routing.project_id and routing.confidence >= 0.80:
                return ClassifiedEvent(
                    email_id=email_id,
                    sender_email=sender_email,
                    subject=subject,
                    classification_type="project_update",
                    project_id=routing.project_id,
                    confidence=routing.confidence,
                    triage_stage_reached=2,
                    reason=f"Multi-project routing: {routing.method}",
                    suppressed_reason="project_update_auto_processed",
                )
            return ClassifiedEvent(
                email_id=email_id,
                sender_email=sender_email,
                subject=subject,
                classification_type="ambiguous",
                confidence=0.5,
                triage_stage_reached=2,
                reason=f"FIX keywords found but {len(identity.project_ids)} projects match domain",
                requires_operator_triage=True,
                should_notify_operator=True,
                notification_reason="multi_project_ambiguous",
            )
        if subject_project_id:
            return ClassifiedEvent(
                email_id=email_id,
                sender_email=sender_email,
                subject=subject,
                classification_type="project_update",
                project_id=subject_project_id,
                confidence=0.80,
                triage_stage_reached=2,
                reason=f"FIX keywords ({keyword_hits} hits) + subject project ID match",
                suppressed_reason="project_update_auto_processed",
            )
        return ClassifiedEvent(
            email_id=email_id,
            sender_email=sender_email,
            subject=subject,
            classification_type="ambiguous",
            confidence=0.5,
            triage_stage_reached=2,
            reason=f"FIX keywords found ({keyword_hits} hits) but no resolvable project",
            requires_operator_triage=True,
            should_notify_operator=True,
            notification_reason="fix_email_unresolved_project",
        )

    return None  # Pass to Stage 3


def has_fix_signal(subject: str, body: str) -> bool:
    combined = (subject + " " + body[:500]).lower()
    return sum(1 for kw in FIX_KEYWORDS if kw in combined) >= 1


def _extract_active_project_id_from_subject(
    conn: sqlite3.Connection,
    subject: str,
) -> str | None:
    from src.email.project_tag_link import resolve_project_id_from_subject

    project_id = resolve_project_id_from_subject(conn, subject)
    if not project_id:
        return None
    row = conn.execute(
        "SELECT 1 FROM projects WHERE project_id = ? AND project_status IN ('Active', 'Waiting', 'Blocked')",
        (project_id,),
    ).fetchone()
    return project_id if row else None


def _has_subject_project_fix_signal(subject: str, body: str) -> bool:
    combined = f"{subject} {body[:500]}".lower()
    return any(hint in combined for hint in SUBJECT_PROJECT_FIX_HINTS)
