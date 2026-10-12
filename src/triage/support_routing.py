"""
Support email routing — creates tickets and evidence from classified support emails.
"""

import logging
import sqlite3

from config.defaults import EMAIL_BODY_SNIPPET_CHARS, EMAIL_BODY_STORE_CHARS

from src.repositories.ticket_repo import (
    canonical_message_id,
    create_ticket,
    find_existing_open_ticket,
    find_ticket_by_thread,
    get_ticket,
)
from src.repositories.ticket_evidence_repo import insert_ticket_evidence
from src.email.inbound_work import InboundRouteClaim
from src.models.support_ticket import SupportNotification
from src.triage.email_triage import ClassifiedEvent

logger = logging.getLogger(__name__)

# Keyword → TicketCategory mapping (first match wins)
_CATEGORY_KEYWORDS: list[tuple[str, list[str]]] = [
    ("fix_session", ["fix session", "logon", "heartbeat", "sequence gap", "resend request", "fix error", "fix reject"]),
    ("network", ["connection refused", "connection timeout", "firewall", "port blocked", "network unreachable", "latency", "packet loss"]),
    ("cert", ["cert", "certificate", "ssl", "tls"]),
    ("trade", ["order reject", "execution report", "trade break", "trade mismatch",
               "not allowed for account", "short sell", "order filter",
               "order not allowed", "position limit"]),
    ("post_trade", ["settlement fail", "allocation fail", "position mismatch"]),
    ("monitoring", ["alert", "monitoring", "degraded", "downtime"]),
    ("access", ["unable to connect", "cannot connect", "access denied"]),
    ("config", ["config", "configuration", "parameter", "tag"]),
]

_SEVERITY_CRITICAL_KW = {"outage", "production issue", "critical issue", "session down"}
_SEVERITY_HIGH_KW = {"urgent issue", "error message", "connection lost", "heartbeat timeout", "logon rejected", "logon failed"}


def _auto_categorize_ticket(subject: str, body: str) -> str:
    """Determine ticket category from email content. Returns category string."""
    combined = (subject + " " + body[:800]).lower()
    for category, keywords in _CATEGORY_KEYWORDS:
        if any(kw in combined for kw in keywords):
            return category
    return "other"


def _auto_severity(subject: str, body: str) -> str:
    """Determine ticket severity heuristically."""
    combined = (subject + " " + body[:800]).lower()
    if any(kw in combined for kw in _SEVERITY_CRITICAL_KW):
        return "critical"
    if any(kw in combined for kw in _SEVERITY_HIGH_KW):
        return "high"
    return "medium"


def _extract_counterparty(conn: sqlite3.Connection, sender_email: str) -> str | None:
    """Try to find a counterparty name from the sender's organisation."""
    from src.triage.identity_resolution import resolve_sender
    identity = resolve_sender(conn, sender_email)
    return identity.organisation_name


def handle_support_email(
    conn: sqlite3.Connection,
    event: ClassifiedEvent,
    body: str,
    *,
    route_claim: InboundRouteClaim | None = None,
    direct_support: bool = False,
) -> SupportNotification | None:
    """
    Process a support-classified email: create ticket + evidence.

    Returns a notification dict for the operator (will be pushed as card).
    """
    from src.email.support_effects import bind_support_route_source

    # Do not create a ticket if a claimed inbound source was lost or a direct
    # caller has not positively declared its ownership.
    bind_support_route_source(
        conn, email_id=event.email_id, route_claim=route_claim,
        direct_support=direct_support,
    )
    category = _auto_categorize_ticket(event.subject, body)
    severity = _auto_severity(event.subject, body)
    counterparty = _extract_counterparty(conn, event.sender_email)

    # Idempotent: email already stored as ticket evidence — do not re-process
    from src.support.pm_queue import is_noise_inbound

    if is_noise_inbound(event.subject, event.sender_email):
        logger.info(
            "Skipping noise support email %s subject=%r sender=%s",
            event.email_id, event.subject, event.sender_email,
        )
        return None

    existing_evidence = conn.execute(
        "SELECT ticket_id FROM ticket_evidence WHERE source_ref = ?",
        (event.email_id,),
    ).fetchone()
    if existing_evidence:
        ticket = get_ticket(conn, existing_evidence[0])
        if ticket:
            logger.debug(
                "Support email %s already recorded on ticket %s — skipping",
                event.email_id, ticket.ticket_id,
            )
            return None

    # Prefer real email-thread linkage over loose project/sender matching
    existing, merge_reason = _resolve_existing_ticket(
        conn, event, counterparty=counterparty, category=category, body=body,
    )

    if existing:
        ticket = existing
        is_followup = True
        logger.info(
            "Appending follow-up to ticket %s: reason=%s sender=%s "
            "message_id=%s subject=%r project=%s category=%s",
            ticket.ticket_id,
            merge_reason,
            event.sender_email,
            event.email_id,
            event.subject,
            event.project_id,
            category,
        )
    else:
        logger.info(
            "Creating support ticket: reason=new_ticket sender=%s message_id=%s "
            "subject=%r project=%s category=%s",
            event.sender_email,
            event.email_id,
            event.subject,
            event.project_id,
            category,
        )
        ticket = create_ticket(
            conn,
            title=event.subject[:200],
            category=category,
            severity=severity,
            source_type="email",
            related_project_id=event.project_id,
            counterparty=counterparty,
            reporter_email=event.sender_email,
            description=body[:EMAIL_BODY_SNIPPET_CHARS],
            thread_anchor=event.email_id,
        )
        is_followup = False

    # Store email as evidence
    ev_label = "Follow-up email" if is_followup else "Inbound email"
    insert_ticket_evidence(
        conn,
        ticket_id=ticket.ticket_id,
        evidence_type="email",
        content_summary=f"{ev_label}: {event.subject[:120]}",
        content_text=body[:EMAIL_BODY_STORE_CHARS],
        source_ref=event.email_id,
    )
    _maybe_store_log_snippet_evidence(conn, ticket.ticket_id, event.subject, body)

    # Stamp ticket_id back onto event and processed_emails for thread linking
    event.ticket_id = ticket.ticket_id
    event.counterparty = counterparty
    conn.execute(
        "UPDATE processed_emails SET ticket_id = ? WHERE email_id = ?",
        (ticket.ticket_id, event.email_id),
    )

    if not is_followup:
        logger.info(
            "Support ticket %s created: category=%s severity=%s counterparty=%s",
            ticket.ticket_id, category, severity, counterparty,
        )
        # Guidance is included in the single new-ticket notification below.
    else:
        logger.info(
            "Follow-up appended to ticket %s from %s",
            ticket.ticket_id, event.sender_email,
        )

    # Run advisory engine for immediate guidance (new tickets only)
    advisory_data = _run_advisory(conn, ticket, body) if not is_followup else None

    # Build notification for operator (Chinese, scannable)
    tag = "📎 跟進" if is_followup else "🆕 新建"
    preview = (body or "").strip()
    # Skip Outlook external-email banner for preview
    for banner in (
        "EXTERNAL EMAIL",
        "外部郵件",
        "Please do not click any link",
    ):
        if banner in preview:
            idx = preview.find("\n", preview.find(banner))
            if idx > 0:
                preview = preview[idx + 1 :].strip()
    preview = preview[:280].replace("\r\n", "\n")
    lines = [
        f"{tag} 工單：`{ticket.ticket_id}`",
        f"分類：{category} · 嚴重性：{severity}",
        f"寄件：{event.sender_email}",
    ]
    if counterparty:
        lines.append(f"對手方：{counterparty}")
    if event.project_id:
        lines.append(f"專案：{event.project_id}")
    if is_followup:
        lines.append(f"原標題：{(ticket.title or '')[:80]}")
    lines.append(f"主旨：{(event.subject or '')[:100]}")
    if preview:
        lines.append("")
        lines.append("📝 摘要")
        lines.append(preview)

    if advisory_data:
        if advisory_data.get("assessment"):
            lines.append(f"\n💡 建議：{advisory_data['assessment']}")
        for step in advisory_data.get("next_steps", []):
            lines.append(f"  → {step}")
        if advisory_data.get("escalation_team"):
            lines.append(f"📞 升級：{advisory_data['escalation_team']}")

    notification = SupportNotification(
        profile="P",
        title=(
            f"支援跟進：{ticket.ticket_id}"
            if is_followup
            else f"新支援工單：{ticket.ticket_id}"
        ),
        body="\n".join(lines),
        project_id=event.project_id,
        ticket_id=ticket.ticket_id,
        advisory=advisory_data,
    )

    from src.email.ticket_notice import build_ticket_email_notice
    notice = build_ticket_email_notice(ticket_id=ticket.ticket_id, sender=event.sender_email,
        subject=event.subject or "", body=body)
    if notice.kind == "delivery_failure":
        notification.title, notification.body = notice.title, notice.body
    send_card = not (is_followup and not notice.immediate and getattr(event, "has_attachments", None) is False)
    if not send_card:
        from src.email.support_effects import enqueue_support_digest
        enqueue_support_digest(
            conn, email_id=event.email_id, route_claim=route_claim,
            direct_support=direct_support,
            category="email_information", title=notice.title, body=notice.body,
            project_id=event.project_id,
            metadata={"action_required": False, "ticket_id": ticket.ticket_id},
        )
    from src.email.support_effects import enqueue_support_effects
    enqueue_support_effects(
        conn, email_id=event.email_id, notification=notification,
        is_followup=is_followup, send_card=send_card,
        route_claim=route_claim, direct_support=direct_support,
    )
    return notification


def _resolve_existing_ticket(
    conn: sqlite3.Connection,
    event: ClassifiedEvent,
    *,
    counterparty: str | None,
    category: str,
    body: str,
) -> tuple[object | None, str]:
    """Resolve follow-up ticket and return the deterministic merge reason."""
    # 1) Email-thread linkage (In-Reply-To / References / prior ticket_id on thread)
    pe = conn.execute(
        """SELECT in_reply_to, references_chain, ticket_id, thread_fp
           FROM processed_emails WHERE email_id = ?""",
        (event.email_id,),
    ).fetchone()
    if pe:
        pe_d = dict(pe)
        if pe_d.get("ticket_id"):
            t = get_ticket(conn, pe_d["ticket_id"])
            if t and t.status in ("open", "investigating", "waiting_external", "waiting_internal"):
                return t, "processed_ticket_id"
        thread_ticket = find_ticket_by_thread(
            conn, pe_d.get("in_reply_to"), pe_d.get("references_chain"),
        )
        if thread_ticket and thread_ticket.status in (
            "open", "investigating", "waiting_external", "waiting_internal",
        ):
            return thread_ticket, "message_thread"
        # Same canonical thread root already linked to a ticket.
        thread_key = canonical_message_id(pe_d.get("thread_fp"))
        if thread_key:
            rows = conn.execute(
                """SELECT ticket_id, thread_fp FROM processed_emails
                   WHERE thread_fp IS NOT NULL AND ticket_id IS NOT NULL
                   ORDER BY processed_at DESC"""
            ).fetchall()
            for row in rows:
                if canonical_message_id(row["thread_fp"]) != thread_key:
                    continue
                t = get_ticket(conn, row["ticket_id"])
                if t and t.status in (
                    "open", "investigating", "waiting_external", "waiting_internal",
                ):
                    return t, "thread_fp"

    # 2) Strict identity match (no category-only or project-only merge)
    ticket = find_existing_open_ticket(
        conn,
        event.sender_email,
        counterparty,
        event.project_id,
        subject=event.subject,
        category=category,
    )
    return (ticket, "subject_match") if ticket else (None, "new_ticket")


def _push_support_operator_card(
    conn: sqlite3.Connection,
    notification: SupportNotification,
    *,
    is_followup: bool,
) -> None:
    """Immediate Telegram card for new support tickets and follow-ups."""
    try:
        from src.telegram.advisory_push import push_support_notify_card_sync

        push_support_notify_card_sync(
            conn,
            ticket_id=notification.ticket_id,
            title=notification.title,
            body=notification.body,
            project_id=notification.project_id,
            is_followup=is_followup,
        )
    except Exception:
        logger.debug(
            "Support operator card push failed for %s",
            notification.ticket_id,
            exc_info=True,
        )


_LOG_MARKERS = (
    "8=fix", "|35=", "35=d", "35=8", "35=a", "heartbeat", "logon", "seqnum",
    "tag 35", "tag 49", "sendercompid", "targetcompid", "session reject",
    "short sell", "order is not allowed", "not allowed for account",
)


def _maybe_store_log_snippet_evidence(
    conn: sqlite3.Connection,
    ticket_id: str,
    subject: str,
    body: str,
) -> None:
    """P2: index FIX-log-like email bodies as searchable log_snippet evidence."""
    combined = f"{subject}\n{body}".lower()
    hits = sum(1 for m in _LOG_MARKERS if m in combined)
    if hits < 2:
        return
    snippet = body.strip()
    if len(snippet) < 40:
        return
    from config.defaults import EMAIL_BODY_STORE_CHARS
    insert_ticket_evidence(
        conn,
        ticket_id=ticket_id,
        evidence_type="log_snippet",
        content_summary=f"FIX log excerpt: {subject[:80]}",
        content_text=snippet[:EMAIL_BODY_STORE_CHARS],
        source_ref=None,
    )


def _maybe_push_support_advisory_card(conn: sqlite3.Connection, ticket) -> None:
    """Push Profile P card with I_HANDLE so the learning loop can start."""
    try:
        from src.advisory.engine import AdvisoryEngine
        from src.telegram.advisory_push import push_support_advisory_card_sync

        engine = AdvisoryEngine()
        advisory = engine.advise_support(conn, ticket)
        if advisory.confidence < 0.5 and not advisory.next_steps:
            return
        push_support_advisory_card_sync(
            conn,
            advisory=advisory,
            ticket_id=ticket.ticket_id,
            ticket_title=ticket.title,
        )
    except Exception:
        logger.debug(
            "Support advisory card push failed for %s", ticket.ticket_id, exc_info=True,
        )


def _run_advisory(conn: sqlite3.Connection, ticket, source: str = "") -> dict | None:
    """Run advisory engine on the new ticket. Returns advisory dict or None."""
    try:
        from src.advisory.engine import AdvisoryEngine
        engine = AdvisoryEngine()
        advisory = engine.advise_support(conn, ticket, event_summary=source)
        return {
            "assessment": advisory.situation_assessment,
            "confidence": advisory.confidence,
            "next_steps": [ns.action for ns in advisory.next_steps[:3]],
            "escalation_team": advisory.escalation.team if advisory.escalation else None,
        }
    except Exception:
        logger.debug("Advisory engine failed for ticket %s", ticket.ticket_id, exc_info=True)
        return None
