"""Handle 📎 Attach to Ticket flow from Profile L (Inbound Reply) card."""

from __future__ import annotations

from collections.abc import Callable
from src.events.bus import Event, event_bus
from src.telegram.i18n import t


def handle_attach(
    conn,
    action: str,
    _project_id,
    *,
    message_context=None,
    operator_id: str = "default_operator",
    post_commit: list[Callable[[], None]] | None = None,
) -> dict:
    """Handle 📎 Attach to Ticket flow from Profile L (Inbound Reply) card.

    Routing (via handle_callback which splits "ATTACH:{action}:{project_id}"):
      PICK  + _project_id=reply_id              → show ticket picker inline keyboard
      SAVE  + _project_id="reply_id:ticket_id"  → record attachment, insert evidence, publish SSE
    """
    from src.repositories.inbound_reply_repo import get_inbound_reply_by_id
    from src.repositories.ticket_evidence_repo import insert_ticket_evidence
    from src.repositories.ticket_repo import get_open_tickets_for_project, get_ticket

    # ── PICK: show open tickets ────────────────────────────────────────────
    if action == "PICK":
        reply_id = _project_id
        reply = get_inbound_reply_by_id(conn, reply_id)
        if reply is None:
            return {"text": t("attach.email_not_found")}

        project_id = reply["project_id"]
        open_tickets = get_open_tickets_for_project(conn, project_id)

        if not open_tickets:
            return {"text": t("attach.no_open_tickets")}

        shown = open_tickets[:5]
        buttons = []
        for tk in shown:
            if hasattr(tk, "ticket_id"):
                tid, title = tk.ticket_id, tk.title
            else:
                tid, title = tk["ticket_id"], tk["title"]
            label = f"{tid}: {title[:30]}"
            buttons.append([{"text": label, "callback_data": f"ATTACH:SAVE:{reply_id}:{tid}"}])

        extra = len(open_tickets) - len(shown)
        note = t("attach.more_tickets").format(count=extra) if extra > 0 else ""

        return {
            "text": t("attach.select_ticket").format(note=note),
            "reply_markup": {"inline_keyboard": buttons},
        }

    # ── SAVE: link email to ticket ─────────────────────────────────────────
    if action == "SAVE" and _project_id and ":" in str(_project_id):
        reply_id, ticket_id = str(_project_id).split(":", 1)

        reply = get_inbound_reply_by_id(conn, reply_id)
        if reply is None:
            return {"text": t("attach.email_not_found")}

        # Callback data is user-controlled; the ticket must still be one that
        # the picker could have offered for this reply's project.
        ticket = get_ticket(conn, ticket_id)
        if ticket is None or ticket.related_project_id != reply["project_id"] or (
            ticket.status not in {"open", "investigating"}
        ):
            return {"_error": True, "text": "Ticket unavailable for this reply. No attachment was saved."}

        # The callback writer is serialized; replay must not duplicate evidence
        # or invalidate the ticket summary a second time.
        existing = conn.execute(
            """SELECT evidence_id FROM ticket_evidence
               WHERE ticket_id = ? AND evidence_type = 'email' AND source_ref = ?
               LIMIT 1""",
            (ticket_id, reply_id),
        ).fetchone()
        if existing:
            return {"text": t("attach.email_attached").format(ticket_id=ticket_id)}

        # Empty thread keys cannot safely correlate processed emails.
        if reply["thread_id"]:
            conn.execute(
                """UPDATE processed_emails SET ticket_id = ?
                   WHERE thread_id = ? AND project_id = ? AND direction = 'inbound'""",
                (ticket_id, reply["thread_id"], reply["project_id"]),
            )

        insert_ticket_evidence(
            conn,
            ticket_id=ticket_id,
            evidence_type="email",
            content_summary=reply["subject"] or "(no subject)",
            content_text=reply["body_text"],
            source_ref=reply_id,
        )
        event = Event(
            event_type="ticket_updated",
            detail={"ticket_id": ticket_id},
            surface="telegram",
        )
        if post_commit is None:
            # Preserve direct-call behavior for legacy callers.
            conn.commit()
            event_bus.publish(event)
        else:
            # The callback caller commits and runs effects after scope exit.
            post_commit.append(lambda: event_bus.publish(event))

        return {"text": t("attach.email_attached").format(ticket_id=ticket_id)}

    return {"text": t("attach.unknown_action")}
