"""Support ticket dashboard endpoints."""

import json
import logging
import sqlite3
from datetime import datetime, timezone
from uuid import uuid4

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel

from src.api.auth import CurrentActor
from src.api.deps import get_conn, get_current_actor, get_write_conn
from src.api.schemas.chat import (
    ChatHistoryResponse,
    ChatSendResponse,
    build_ephemeral_turn,
    chat_capabilities,
    normalize_session_messages,
)
from src.auth.rbac import is_read_only_actor
from src.api.schemas.support import (
    SupportTicketUpdateRequest,
    SupportTicketActionRequest,
    SupportChatRequest,
)
from src.repositories.support_chat_repo import get_chat_messages
from src.repositories.ticket_repo import (
    get_ticket,
    get_tickets_by_status,
    search_tickets,
    update_ticket_fields,
    get_ticket_conversation,
    derive_ball_state,
    resolve_ticket_envelope,
)
from src.repositories.ticket_activity_repo import get_ticket_activity
from src.repositories.ticket_evidence_repo import get_evidence_for_ticket
from src.support.web_agent import support_web_agent
from src.events.bus import Event, event_bus
from src.db.connection import scoped_write

router = APIRouter(tags=["support"])
logger = logging.getLogger(__name__)


def _ticket_to_dict(ticket, conn: sqlite3.Connection | None = None) -> dict:
    """Convert a SupportTicket model to a plain dict with string enum values."""
    data = ticket.model_dump()
    for key, val in data.items():
        if hasattr(val, "value"):
            data[key] = val.value
        elif isinstance(val, object) and hasattr(val, "isoformat"):
            data[key] = val.isoformat() if val is not None else None
    org_id = data.get("counterparty_org_id")
    if conn is not None and org_id:
        row = conn.execute(
            "SELECT org_name FROM organisations WHERE org_id = ?",
            (org_id,),
        ).fetchone()
        data["counterparty_org_name"] = row["org_name"] if row else None
    else:
        data.setdefault("counterparty_org_name", None)
    return data


def _evidence_to_dict(ev) -> dict:
    data = ev.model_dump()
    for key, val in data.items():
        if hasattr(val, "isoformat") and val is not None:
            data[key] = val.isoformat()
    return data


@router.get("/support")
def list_support_tickets(
    status: str | None = None,
    query: str | None = None,
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    conn: sqlite3.Connection = Depends(get_conn),
):
    """List support tickets with optional filtering."""
    if query:
        tickets = search_tickets(conn, query, limit=limit + offset)
    elif status:
        tickets = get_tickets_by_status(conn, status, limit=limit + offset)
    else:
        tickets = get_tickets_by_status(conn, "open", limit=limit + offset)

    sliced = tickets[offset: offset + limit]
    # Return a true total count for pagination, not the already-capped fetch count.
    # search_tickets / get_tickets_by_status do not expose a total, so we use
    # the capped fetch as an upper-bound estimate and add a has_more flag.
    has_more = len(tickets) >= (limit + offset)
    return {
        "tickets": [_ticket_to_dict(t, conn) for t in sliced],
        "total": len(tickets) if not has_more else None,
        "has_more": has_more,
        "offset": offset,
        "limit": limit,
    }


@router.get("/support/{ticket_id}")
def get_support_ticket(
    ticket_id: str,
    conn: sqlite3.Connection = Depends(get_conn),
):
    """Get a single support ticket by ID."""
    ticket = get_ticket(conn, ticket_id)
    if not ticket:
        raise HTTPException(404, f"Ticket {ticket_id} not found")
    return _ticket_to_dict(ticket, conn)


@router.get("/support/{ticket_id}/detail")
def get_support_ticket_detail(
    ticket_id: str,
    conn: sqlite3.Connection = Depends(get_conn),
):
    """Enriched detail payload for the PM view."""
    ticket = get_ticket(conn, ticket_id)
    if not ticket:
        raise HTTPException(404, f"Ticket {ticket_id} not found")

    ball = derive_ball_state(conn, ticket_id)
    conversation = get_ticket_conversation(conn, ticket_id)
    activity = get_ticket_activity(conn, ticket_id)

    # Follow-up info
    followup_info: dict = {"has_followup": False}
    if ticket.related_project_id:
        try:
            from src.portfolio.followup_tracker import get_open_followups
            fups = get_open_followups(conn, ticket.related_project_id)
            if fups:
                nearest = min(fups, key=lambda f: f.get("due_date") or "9999")
                followup_info = {
                    "has_followup": True,
                    "followup_id": nearest.get("followup_id"),
                    "due_date": nearest.get("due_date"),
                    "description": nearest.get("description"),
                }
        except Exception:
            pass

    # Project link
    project_link: dict = {"has_project": False}
    if ticket.related_project_id:
        try:
            from src.repositories.project_repo import get_project
            proj = get_project(conn, ticket.related_project_id)
            if proj:
                ms = proj.current_milestone
                project_link = {
                    "has_project": True,
                    "project_id": proj.project_id,
                    "project_name": proj.client_name,
                    "counterparty": proj.client_name,
                    "current_milestone": ms if isinstance(ms, str) else (ms.value if ms else None),
                }
        except Exception:
            pass

    td = _ticket_to_dict(ticket, conn)
    return {
        **{k: td.get(k) for k in [
            "ticket_id", "title", "category", "severity", "status",
            "source_type", "counterparty", "counterparty_org_id",
            "counterparty_org_name", "reporter_email",
            "assigned_team", "escalated_to", "description",
            "root_cause", "resolution", "created_at", "updated_at",
        ]},
        "ball": ball,
        "conversation": conversation,
        "followup": followup_info,
        "activity": activity,
        "project": project_link,
        "ai_summary": None,
        "reply_draft_text": td.get("reply_draft_text"),
    }


@router.patch("/support/{ticket_id}")
def update_support_ticket(
    ticket_id: str,
    body: SupportTicketUpdateRequest,
    conn: sqlite3.Connection = Depends(get_write_conn),
):
    """Update fields on a support ticket."""
    ticket = get_ticket(conn, ticket_id)
    if not ticket:
        raise HTTPException(404, f"Ticket {ticket_id} not found")

    updates = {k: v for k, v in body.model_dump().items() if v is not None}
    if not updates:
        raise HTTPException(400, "No fields to update")

    updated = update_ticket_fields(conn, ticket_id, **updates)
    conn.commit()
    if not updated:
        raise HTTPException(422, "Update failed")
    return {"success": True, "ticket_id": ticket_id, "updated_fields": list(updates.keys())}


@router.get("/support/{ticket_id}/summary")
def get_ticket_ai_summary(
    ticket_id: str,
    force: bool = False,
    conn: sqlite3.Connection = Depends(get_conn),
):
    """Return an AI-generated from/to/what summary for a support ticket.

    The result is computed once via LLM and cached in ai_summary_json.
    Subsequent calls return the cached value instantly. Use ?force=true to
    bust the cache and regenerate.
    """
    ticket = get_ticket(conn, ticket_id)
    if not ticket:
        raise HTTPException(404, f"Ticket {ticket_id} not found")

    counterparty = ticket.counterparty or "Unknown"
    assigned = ticket.assigned_team or "Support Team"

    # Return cached summary if available and not forcing a refresh
    if ticket.ai_summary_json and not force:
        try:
            cached = json.loads(ticket.ai_summary_json)
            return {
                "ticket_id": ticket_id,
                "from_party": cached.get("from_party") or counterparty,
                "to_party": cached.get("to_party") or assigned,
                "what_happened": cached.get("what_happened") or ticket.title,
                "reply_draft_text": cached.get("reply_draft") or ticket.reply_draft_text or "",
                "recommended_action": cached.get("recommended_action") or "send_reply",
                "cached": True,
            }
        except (json.JSONDecodeError, AttributeError):
            pass  # corrupted cache — fall through to regenerate

    evidence = get_evidence_for_ticket(conn, ticket_id)
    ev_lines = "\n".join(
        f"- [{e.evidence_type}] {e.content_summary}" for e in evidence[:5]
    )
    desc = (ticket.description or "")[:1500]

    # Build conversation thread (newest-first) so the LLM sees the full state
    from src.repositories.ticket_repo import get_ticket_conversation, derive_ball_state
    conv = get_ticket_conversation(conn, ticket_id)
    conv_lines = "\n".join(
        f"- [{m['direction'].upper()}] {m.get('sender_email') or m.get('sender_name') or 'Unknown'} ({m['timestamp']}): {m['preview'][:200]}"
        for m in conv[:10]
    ) or "No messages yet"

    # Compute staleness: days since last outbound with no client reply
    today = datetime.now(timezone.utc)
    ball = derive_ball_state(conn, ticket_id)
    days_waiting = None
    staleness_note = ""
    if ball.get("ball_with") == "External" and ball.get("since"):
        try:
            since_dt = datetime.fromisoformat(ball["since"].replace("Z", "+00:00"))
            if since_dt.tzinfo is None:
                since_dt = since_dt.replace(tzinfo=timezone.utc)
            days_waiting = (today - since_dt).days
            if days_waiting >= 3:
                staleness_note = (
                    f"\nSTALENESS ALERT: We sent our last reply {days_waiting} days ago "
                    f"and the client has not responded since. "
                    f"Common sense suggests the issue is likely self-resolved — "
                    f"clients who still have problems almost always follow up. "
                    f"Reflect this in what_happened and suggest closing the ticket in reply_draft."
                )
        except (ValueError, TypeError):
            pass

    prompt = (
        f"Today's date: {today.strftime('%Y-%m-%d')}\n"
        f"Ticket: {ticket.ticket_id}  Title: {ticket.title}\n"
        f"Category: {ticket.category}  Severity: {ticket.severity}\n"
        f"Status: {ticket.status}  Counterparty: {counterparty}  Assigned to: {assigned}\n"
        f"Description:\n{desc}\n"
        f"Evidence:\n{ev_lines or 'None'}\n"
        f"Conversation (newest first):\n{conv_lines}"
        f"{staleness_note}\n\n"
        "Based on the FULL conversation above (not just the first message), "
        "return JSON with exactly 5 keys:\n"
        '  "from_party": name + company of who raised the issue\n'
        '  "to_party": who it is addressed to or needs to handle it\n'
        '  "what_happened": 1-2 sentence summary of the CURRENT state of this ticket '
        "(what was requested, what has been done so far, what is pending)\n"
        '  "reply_draft": a professional reply email draft that addresses the LATEST '
        "message in the conversation. If you (the assigned team) already replied and "
        "are waiting for the client, draft a follow-up. If a new inbound arrived, "
        "draft a response to THAT message. 2-4 sentences.\n"
        '  "recommended_action": exactly one of these three values — no other values allowed:\n'
        '    "close_directly" — the issue is resolved and no reply is needed. Use when the client '
        'sent a closure signal ("noted with thanks", "confirmed", "no further issues", '
        '"problem solved") OR we sent a reply ≥3 days ago and the client never followed up '
        "(silence = resolved).\n"
        '    "send_reply" — a new inbound message requires a response, or we need to '
        "confirm something with the client before closing.\n"
        '    "investigate" — the root cause is unclear, more diagnostic info is needed, '
        "or the issue is actively ongoing with no resolution yet."
    )
    try:
        from src.llm.client import llm_call_structured  # local import, optional dep
        result = llm_call_structured(
            "support_summary",
            [{"role": "user", "content": prompt}],
            {"from_party": "str", "to_party": "str", "what_happened": "str",
             "reply_draft": "str", "recommended_action": "str"},
        )
        valid_actions = {"send_reply", "close_directly", "investigate"}
        raw_action = result.get("recommended_action") or ""
        recommended_action = raw_action if raw_action in valid_actions else "send_reply"
        summary_data = {
            "from_party": result.get("from_party") or counterparty,
            "to_party": result.get("to_party") or assigned,
            "what_happened": result.get("what_happened") or ticket.title,
            "reply_draft": result.get("reply_draft") or "",
            "recommended_action": recommended_action,
        }
        # Persist so future loads are instant
        with scoped_write() as wconn:
            update_ticket_fields(
                wconn, ticket_id,
                ai_summary_json=json.dumps(summary_data),
                reply_draft_text=summary_data["reply_draft"] or None,
            )
        return {
            "ticket_id": ticket_id,
            "from_party": summary_data["from_party"],
            "to_party": summary_data["to_party"],
            "what_happened": summary_data["what_happened"],
            "reply_draft_text": summary_data["reply_draft"],
            "recommended_action": summary_data["recommended_action"],
            "cached": False,
        }
    except Exception:
        # Deterministic fallback — cache it so the page doesn't retry the LLM every load
        fallback = {
            "from_party": counterparty,
            "to_party": assigned,
            "what_happened": (ticket.description or ticket.title)[:200],
        }
        with scoped_write() as wconn:
            update_ticket_fields(
                wconn, ticket_id,
                ai_summary_json=json.dumps(fallback),
            )
        return {
            "ticket_id": ticket_id,
            **fallback,
            "cached": False,
        }


@router.get("/support/{ticket_id}/evidence")
def list_ticket_evidence(
    ticket_id: str,
    conn: sqlite3.Connection = Depends(get_conn),
):
    """Get all evidence for a support ticket."""
    ticket = get_ticket(conn, ticket_id)
    if not ticket:
        raise HTTPException(404, f"Ticket {ticket_id} not found")

    evidence = get_evidence_for_ticket(conn, ticket_id)
    return {
        "evidence": [_evidence_to_dict(e) for e in evidence],
        "total": len(evidence),
    }


# ---------------------------------------------------------------------------
# Action endpoint — flat dispatch for ticket lifecycle operations
# ---------------------------------------------------------------------------

def _action_investigate(conn, ticket, params: dict) -> dict:
    update_ticket_fields(conn, ticket.ticket_id, status="investigating")
    conn.commit()
    return {"status": "investigating"}


def _action_resolve(conn, ticket, params: dict) -> dict:
    updates: dict = {"status": "resolved",
                     "resolved_at": datetime.now(timezone.utc).isoformat()}
    if params.get("resolution"):
        updates["resolution"] = params["resolution"]
    update_ticket_fields(conn, ticket.ticket_id, **updates)
    conn.commit()
    return {"status": "resolved"}


def _action_reopen(conn, ticket, params: dict) -> dict:
    update_ticket_fields(conn, ticket.ticket_id, status="open")
    conn.commit()
    return {"status": "open"}


def _action_route(conn, ticket, params: dict) -> dict:
    team = params.get("team", "")
    routing_email = params.get("routing_email", "")
    if routing_email and ("@" not in routing_email or len(routing_email) > 254):
        routing_email = ""  # ignore invalid address silently
    update_ticket_fields(conn, ticket.ticket_id, assigned_team=team)
    conn.commit()
    if routing_email:
        _send_routing_email(ticket, team, routing_email)
    return {"assigned_team": team}


def _send_routing_email(ticket, team: str, to_email: str) -> None:
    """Send internal routing notification. Silent on SMTP errors."""
    import smtplib
    from email.mime.text import MIMEText
    from config.settings import CONFIG
    smtp_host = getattr(CONFIG, "smtp_host", "") or getattr(CONFIG.email, "smtp_host", "")
    if not to_email or not smtp_host:
        return
    from_addr = (
        getattr(CONFIG, "support_routing_email", "")
        or getattr(CONFIG.email, "from_email", "")
    )
    subject = f"[Support Ticket {ticket.ticket_id}] Assigned to {team}: {ticket.title[:60]}"
    body = (
        f"Ticket {ticket.ticket_id} has been assigned to: {team}\n\n"
        f"Title: {ticket.title}\n"
        f"Counterparty: {ticket.counterparty or 'Unknown'}\n"
        f"Category: {ticket.category}\n"
        f"Severity: {ticket.severity}\n\n"
        f"Description:\n{(ticket.description or '')[:500]}\n\n"
        f"Dashboard: {getattr(CONFIG, 'dashboard_base_url', '')}/support/{ticket.ticket_id}"
    )
    msg = MIMEText(body)
    msg["Subject"] = subject
    msg["From"] = from_addr
    msg["To"] = to_email
    smtp_port = getattr(CONFIG.email, "smtp_port", 587)
    try:
        with smtplib.SMTP(smtp_host, smtp_port) as s:
            s.sendmail(from_addr, [to_email], msg.as_string())
    except Exception:
        import logging
        logging.getLogger(__name__).warning(
            "Routing email send failed to %s for ticket %s", to_email, ticket.ticket_id
        )


def _publish_ticket_updated(ticket_id: str) -> None:
    """Fire a ticket_updated SSE event to all dashboard subscribers."""
    event_bus.publish(Event(
        event_type="ticket_updated",
        detail={"ticket_id": ticket_id},
        surface="system",
    ))


def _create_ticket_draft(conn, ticket, body_text: str | None = None) -> str:
    envelope = resolve_ticket_envelope(conn, ticket.ticket_id)
    ai_text = (body_text if body_text is not None else ticket.reply_draft_text or "").strip()
    quoted = envelope["quoted_body"]
    if ai_text and quoted:
        body = f"{ai_text}\n\n{quoted}"
    elif ai_text:
        body = ai_text
    elif quoted:
        body = f"\n\n{quoted}"
    else:
        body = ""
    from src.email.draft_session import create_draft

    return create_draft(
        conn,
        project_id=ticket.related_project_id,
        task_type="support_reply",
        subject=envelope["subject"],
        body=body,
        to_email=envelope["to_email"],
        cc=envelope["cc"],
        context_packet_ref=envelope["in_reply_to"],
        ticket_id=ticket.ticket_id,
    )


def _action_send_followup(conn, ticket, params: dict) -> dict:
    draft_id = _create_ticket_draft(conn, ticket, params.get("body_text"))
    conn.commit()
    return {"draft_id": draft_id}


def _action_close_stale(conn, ticket, params: dict) -> dict:
    resolution = (params.get("resolution") or "").strip() or "Closed as stale from the desk queue."
    return _action_resolve(conn, ticket, {"resolution": resolution})


def _action_not_an_issue(conn, ticket, params: dict) -> dict:
    resolution = (params.get("resolution") or "").strip() or "Not a desk issue."
    return _action_resolve(conn, ticket, {"resolution": resolution})


def _action_add_note(conn, ticket, params: dict) -> dict:
    """Add an internal note as ticket evidence."""
    text = (params.get("text") or "").strip()
    if not text:
        raise HTTPException(422, "Note text is required and must not be empty")
    from src.repositories.ticket_evidence_repo import insert_ticket_evidence
    eid = insert_ticket_evidence(
        conn, ticket.ticket_id, "note",
        f"Operator note: {text[:500]}",
        content_text=text,
    )
    conn.commit()
    _publish_ticket_updated(ticket.ticket_id)
    return {"evidence_id": eid}


class CreateDraftRequest(BaseModel):
    body_text: str | None = None


@router.post("/support/{ticket_id}/create-draft")
def create_support_draft(
    ticket_id: str,
    req: CreateDraftRequest = CreateDraftRequest(),
    conn: sqlite3.Connection = Depends(get_write_conn),
):
    """Create a draft from a support ticket for review before sending."""
    ticket = get_ticket(conn, ticket_id)
    if not ticket:
        raise HTTPException(404, f"Ticket {ticket_id} not found")

    draft_id = _create_ticket_draft(conn, ticket, req.body_text)
    return {"draft_id": draft_id}


@router.post("/support/bust-summary-cache")
def bust_all_summary_caches(
    conn: sqlite3.Connection = Depends(get_write_conn),
):
    """Clear ai_summary_json for all open tickets, forcing fresh LLM regeneration on next load."""
    result = conn.execute(
        """UPDATE support_tickets
           SET ai_summary_json = NULL, reply_draft_text = NULL
           WHERE status IN ('open', 'investigating', 'waiting_external', 'waiting_internal')"""
    )
    conn.commit()
    return {"cleared": result.rowcount}


_ACTION_HANDLERS = {
    "investigate": _action_investigate,
    "resolve": _action_resolve,
    "reopen": _action_reopen,
    "route": _action_route,
    "add_note": _action_add_note,
    "send_followup": _action_send_followup,
    "close_stale": _action_close_stale,
    "not_an_issue": _action_not_an_issue,
}


@router.post("/support/{ticket_id}/action")
def support_ticket_action(
    ticket_id: str,
    body: SupportTicketActionRequest,
    conn: sqlite3.Connection = Depends(get_write_conn),
):
    """Flat action dispatch for support ticket lifecycle."""
    ticket = get_ticket(conn, ticket_id)
    if not ticket:
        raise HTTPException(404, f"Ticket {ticket_id} not found")
    handler = _ACTION_HANDLERS.get(body.action_type)
    if handler is None:
        raise HTTPException(422, f"Unknown action: {body.action_type}")
    result = handler(conn, ticket, body.params)
    return {"success": True, "ticket_id": ticket_id, **result}


@router.get(
    "/support/{ticket_id}/chat",
    response_model=ChatHistoryResponse,
)
def list_chat_history(
    ticket_id: str,
    conn: sqlite3.Connection = Depends(get_conn),
    actor: CurrentActor = Depends(get_current_actor),
):
    """Return the authenticated user's support chat history and capabilities."""
    ticket = get_ticket(conn, ticket_id)
    if not ticket:
        raise HTTPException(404, f"Ticket {ticket_id} not found")
    read_only = is_read_only_actor(actor)
    messages = get_chat_messages(conn, ticket_id, owner_user_id=actor.user_id)
    return ChatHistoryResponse(
        messages=messages,
        capabilities=chat_capabilities(read_only=read_only),
    )


@router.post(
    "/support/{ticket_id}/chat",
    response_model=ChatSendResponse,
)
def chat_with_agent(
    ticket_id: str,
    body: SupportChatRequest,
    conn: sqlite3.Connection = Depends(get_conn),
    actor: CurrentActor = Depends(get_current_actor),
):
    """Run the support agent with backend-enforced actor execution policy."""
    if not get_ticket(conn, ticket_id):
        raise HTTPException(404, f"Ticket {ticket_id} not found")
    read_only = is_read_only_actor(actor)
    if body.session_messages and not read_only:
        raise HTTPException(
            status_code=400,
            detail="session_messages are accepted only for read-only chat sessions",
        )

    active_turn_id = uuid4().hex
    session_messages = (
        normalize_session_messages(body.session_messages) if read_only else []
    )
    try:
        reply = support_web_agent(
            conn,
            ticket_id,
            body.message,
            use_scoped_writes=True,
            operator_id=actor.user_id,
            dashboard_permissions=actor.permissions,
            read_only_execution=read_only,
            persist_messages=not read_only,
            session_messages=[message.model_dump(exclude_none=True) for message in session_messages],
            turn_id=active_turn_id,
        )
    except ValueError as exc:
        raise HTTPException(404, str(exc)) from exc
    except Exception:
        logger.exception("Support agent error for %s", ticket_id)
        raise HTTPException(500, "Agent error: request could not be completed")

    if read_only:
        messages = build_ephemeral_turn(
            session_messages,
            user_message=body.message,
            reply=reply,
            turn_id=active_turn_id,
        )
    else:
        messages = get_chat_messages(conn, ticket_id, owner_user_id=actor.user_id)
    return ChatSendResponse(
        reply=reply,
        messages=messages,
        active_turn_id=active_turn_id,
        capabilities=chat_capabilities(read_only=read_only),
    )
