"""
Support ticket chat handler functions — runtime execution for support tools.
"""

from __future__ import annotations

import sqlite3
from typing import Any

from src.repositories.ticket_repo import (
    create_ticket,
    get_ticket,
    search_tickets,
    get_tickets_by_status,
    update_ticket_fields,
)
from src.telegram.i18n import t
from src.utils.enum_helpers import enum_val


def chat_create_ticket(conn: sqlite3.Connection, args: dict) -> dict:
    """Create a new support ticket."""
    title = args.get("title", "").strip()
    category = args.get("category", "other").strip()
    if not title:
        return {"text": t("support.create_title_required")}

    severity = args.get("severity", "medium").strip()
    ticket = create_ticket(
        conn,
        title=title,
        category=category,
        severity=severity,
        description=args.get("description"),
        counterparty=args.get("counterparty"),
        related_project_id=args.get("related_project_id"),
        source_type="manual",
    )
    conn.commit()
    return {
        "text": t("support.ticket_created").format(
            ticket_id=ticket.ticket_id,
            title=ticket.title,
            category=category,
            severity=severity,
        )
    }


def chat_update_ticket(conn: sqlite3.Connection, args: dict) -> dict:
    """Update a field on a support ticket."""
    ticket_id = args.get("ticket_id", "").strip()
    field = args.get("field", "").strip()
    value = args.get("value", "").strip()

    if not ticket_id or not field:
        return {"text": t("support.update_missing_fields")}

    allowed_fields = {
        "status", "severity", "assigned_team", "escalated_to",
        "root_cause", "resolution", "description", "category",
        "counterparty", "related_project_id",
    }
    if field not in allowed_fields:
        return {"text": t("support.update_invalid_field").format(field=field, valid_fields=', '.join(sorted(allowed_fields)))}

    existing = get_ticket(conn, ticket_id)
    if not existing:
        return {"text": t("support.ticket_not_found").format(ticket_id=ticket_id)}

    updated = update_ticket_fields(conn, ticket_id, **{field: value})
    conn.commit()
    if updated:
        return {"text": t("support.ticket_updated").format(ticket_id=ticket_id, field=field, value=value)}
    return {"text": t("support.ticket_update_failed").format(ticket_id=ticket_id)}


def chat_search_tickets(conn: sqlite3.Connection, args: dict) -> dict:
    """Search support tickets."""
    query = args.get("query", "").strip()
    status = args.get("status", "").strip()
    category = args.get("category", "").strip()
    limit = int(args.get("limit", 10))

    desk_queues = {"", "open", "needs_me", "waiting", "stale", "noise", "all"}
    if not query and not category and status in desk_queues:
        from src.support.desk_brief import collect_desk_snapshot

        queue = "needs_me" if status in {"", "open"} else status
        snapshot = collect_desk_snapshot(conn, list_queue=queue, limit=limit)
        if snapshot.all_active == 0:
            return {"text": t("support.no_tickets")}
        rows = snapshot.needs if queue == "needs_me" else snapshot.listed
        lines = [
            t("support.desk_header").format(
                needs=snapshot.needs_count,
                waiting=snapshot.waiting_count,
                stale=snapshot.stale_count,
            ),
            t("support.tickets_found").format(count=snapshot.all_active if queue in {"", "open", "all"} else len(rows)),
        ]
        for item in rows:
            who = item.party_name or "Unidentified"
            lines.append(
                f"  [{item.ticket_id}] {item.display_title[:60]}\n"
                f"    {item.case_type} | {who} | {item.next_label}"
                + (f" | {item.age_days}d" if item.age_days is not None else "")
            )
        if queue in {"", "open", "needs_me"} and snapshot.stale_count:
            lines.append(t("support.desk_stale_hint").format(stale=snapshot.stale_count))
        return {"text": "\n".join(lines)}

    if query:
        tickets = search_tickets(conn, query, limit=limit)
    elif status:
        tickets = get_tickets_by_status(conn, status, limit=limit)
    else:
        tickets = get_tickets_by_status(conn, "open", limit=limit)

    if category and tickets:
        tickets = [tk for tk in tickets if enum_val(tk.category) == category]

    if not tickets:
        return {"text": t("support.no_tickets")}

    lines = [t("support.tickets_found").format(count=len(tickets))]
    for tk in tickets[:10]:
        sev = enum_val(tk.severity)
        cat = enum_val(tk.category)
        status_val = enum_val(tk.status)
        lines.append(
            f"  [{tk.ticket_id}] {tk.title[:60]}\n"
            f"    {cat} | {sev} | {status_val}"
            + (f" | {tk.counterparty}" if tk.counterparty else "")
        )
    return {"text": "\n".join(lines)}


def chat_get_advisory(conn: sqlite3.Connection, args: dict) -> dict:
    """Get advisory for a ticket or project."""
    ticket_id = args.get("ticket_id", "").strip()
    project_id = args.get("project_id", "").strip()

    from src.advisory.engine import AdvisoryEngine
    engine = AdvisoryEngine()

    if ticket_id:
        ticket = get_ticket(conn, ticket_id)
        if not ticket:
            return {"text": t("support.advisory_ticket_not_found").format(ticket_id=ticket_id)}
        advisory = engine.advise_support(conn, ticket)
        return _format_advisory_text(advisory, f"ticket {ticket_id}")

    if project_id:
        from src.context.governed_memory import authorize_project_operator
        operator_id = args.get("_operator_id")
        if not authorize_project_operator(conn, project_id, operator_id):
            return {"text": "Unauthorized advisory request."}

        from src.repositories.project_repo import get_project
        project = get_project(conn, project_id)
        if not project:
            return {"text": t("support.advisory_project_not_found").format(project_id=project_id)}
        milestone = project.current_milestone or ""
        ms_val = enum_val(milestone)
        bw = project.ball_with
        bw_val = enum_val(bw) if bw else None
        advisory = engine.advise_project(
            conn, project_id, ms_val, ball_with=bw_val,
            operator_id=operator_id,
        )
        return _format_advisory_text(advisory, f"project {project_id}")

    return {"text": t("support.advisory_missing_id")}


def chat_record_resolution(conn: sqlite3.Connection, args: dict) -> dict:
    """Record a resolution and extract a reusable pattern."""
    resolution_text = args.get("resolution_text", "").strip()
    if not resolution_text:
        return {"text": t("support.resolution_text_required")}

    ticket_id = args.get("ticket_id", "").strip() or None
    category = args.get("category")

    ticket_context = None
    if ticket_id:
        ticket = get_ticket(conn, ticket_id)
        if ticket:
            ticket_context = _ticket_to_context(ticket)
            if not category:
                category = ticket_context["category"]

    from src.advisory.resolution_learner import extract_resolution_pattern, store_resolution
    pattern_data = extract_resolution_pattern(resolution_text, ticket_context)
    if not pattern_data:
        return {"text": t("support.resolution_no_pattern")}

    if category:
        pattern_data["category"] = category

    pattern = store_resolution(conn, pattern_data, ticket_id=ticket_id)

    # If ticket exists, mark it resolved
    if ticket_id:
        update_ticket_fields(conn, ticket_id, status="resolved", resolution=resolution_text[:2000])

    conn.commit()

    return {
        "text": (
            t("support.resolution_stored").format(
                pattern_id=pattern.pattern_id,
                category=pattern.category or 'unknown',
                confidence=f"{pattern.confidence:.0%}",
            )
            + (t("support.resolution_ticket_resolved").format(ticket_id=ticket_id) if ticket_id else "")
        )
    }


def _ticket_to_context(ticket) -> dict:
    """Build a ticket context dict for resolution extraction."""
    return {
        "category": enum_val(ticket.category),
        "severity": enum_val(ticket.severity),
        "title": ticket.title,
        "description": ticket.description or "",
    }


def _format_advisory_text(advisory, context_label: str) -> dict:
    """Format an Advisory into readable chat text."""
    lines = [t("support.advisory_header").format(context_label=context_label, confidence=f"{advisory.confidence:.0%}")]
    lines.append(f"  {advisory.situation_assessment}")

    if advisory.next_steps:
        lines.append("\n" + t("support.advisory_next_steps"))
        for i, ns in enumerate(advisory.next_steps[:5], 1):
            priority_icon = {"now": "🔴", "soon": "🟡", "when_ready": "🟢"}.get(ns.priority, "▪️")
            lines.append(f"  {priority_icon} {i}. {ns.action}")

    if getattr(advisory, "criterion_results", None):
        lines.append("\nCriterion results:")
        for item in advisory.criterion_results:
            lines.append(f"  - {item['criterion_id']}={item['result']}")
    lines.append("\nDB conflict:")
    if getattr(advisory, "db_conflicts", None):
        lines.extend(f"  - {item}" for item in advisory.db_conflicts)
    else:
        lines.append("  - none")
    if getattr(advisory, "evidence_ref", None):
        lines.append("\nEvidence citations:")
        lines.extend(f"  - {item}" for item in advisory.evidence_ref)
    if getattr(advisory, "policy_ref", None):
        lines.append("\nPolicy references:")
        lines.extend(f"  - {item}" for item in advisory.policy_ref)

    if advisory.escalation:
        lines.append("\n" + t("support.advisory_escalation").format(team=advisory.escalation.team, reason=advisory.escalation.reason))

    if advisory.similar_past_cases:
        lines.append("\n" + t("support.advisory_similar_cases").format(cases=', '.join(advisory.similar_past_cases[:3])))

    return {"text": "\n".join(lines)}


def chat_create_outreach_email_draft(
    conn: sqlite3.Connection,
    args: dict,
) -> dict:
    """Create an OUTREACH ticket and draft for operator approval in Dashboard."""
    from config.settings import CONFIG
    from src.email.reference_library import get_reference_doc, resolve_doc_path

    to_email = (args.get("to_email") or "").strip()
    counterparty = (args.get("counterparty") or "").strip()
    subject = (args.get("subject") or "").strip()
    body = (args.get("body") or "").strip()
    cc_list: list[str] = [c.strip() for c in (args.get("cc") or []) if c.strip()]
    doc_key = (args.get("doc_key") or "").strip() or None

    if not to_email or not counterparty or not subject or not body:
        return {
            "ok": False,
            "error": "missing_required_fields",
            "text": "Missing required fields: to_email, counterparty, subject, body.",
        }

    doc_meta = None
    doc_path = None
    if doc_key:
        doc_meta = get_reference_doc(doc_key)
        if not doc_meta:
            return {
                "ok": False,
                "error": "unknown_reference_doc",
                "text": (
                    f"Unknown doc_key '{doc_key}'. Call list_reference_docs "
                    "to see available documents."
                ),
            }
        doc_path = resolve_doc_path(doc_key)
        if not doc_path or not doc_path.exists():
            return {
                "ok": False,
                "error": "reference_doc_file_missing",
                "text": (
                    f"File for '{doc_key}' not found on disk. "
                    "Check data/reference_docs/."
                ),
            }

    from src.services.outreach_draft_service import create_outreach_draft

    receipt = create_outreach_draft(
        conn, to_email=to_email, counterparty=counterparty,
        subject=subject, body=body, cc_list=cc_list, data_dir=CONFIG.data_dir,
        doc_filename=doc_meta["filename"] if doc_meta else None,
        doc_path=doc_path,
    )

    attachment_note = f" + {receipt.attachment_filename}" if receipt.attachment_filename else ""
    cc_note = f", CC: {', '.join(cc_list)}" if cc_list else ""
    return {
        "ok": True,
        "text": (
            f"Outreach draft created for {to_email}{cc_note}{attachment_note}.\n"
            f"Draft: {receipt.draft_id} | Ticket: {receipt.ticket_id}\n"
            "Review and approve in Dashboard Drafts before sending."
        ),
        "draft_id": receipt.draft_id,
        "ticket_id": receipt.ticket_id,
        "outcome": "draft_created",
        "committed": True,
    }


def chat_send_outreach_email(conn: sqlite3.Connection, args: dict) -> dict:
    """Deprecated compatibility alias; creates a draft and never sends email."""
    result = chat_create_outreach_email_draft(conn, args)
    return {**result, "deprecated_alias": "send_outreach_email"}


SUPPORT_CHAT_TOOL_HANDLERS: dict[str, Any] = {
    "create_ticket": chat_create_ticket,
    "update_ticket": chat_update_ticket,
    "search_tickets": chat_search_tickets,
    "get_advisory": chat_get_advisory,
    "record_resolution": chat_record_resolution,
    "create_outreach_email_draft": chat_create_outreach_email_draft,
    "send_outreach_email": chat_send_outreach_email,
}
