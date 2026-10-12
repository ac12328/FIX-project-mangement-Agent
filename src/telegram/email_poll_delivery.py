"""Presentation-only delivery helpers for the scheduled IMAP poll.

The route ledger records local command boundaries, not an end-to-end
Telegram delivery guarantee. Interrupted sends still require reconciliation;
they must not be replayed automatically.
"""
from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass

from src.email.inbox_fetcher import FetchedEmail
from src.email.inbound_work import InboundRouteClaim, RouteDeliveryIntent
from src.models.support_ticket import SupportTicket
from src.telegram.email_poll_ports import EmailPollPorts


def _checkpoint(
    conn, claim: InboundRouteClaim, step: str, status: str, *,
    delivery_intent: RouteDeliveryIntent | None = None,
    delivery_message_ids: list[int] | None = None,
) -> None:
    from src.email.inbound_work import checkpoint_route_step

    delivery = {}
    if delivery_intent is not None:
        delivery["delivery_intent"] = delivery_intent
    if delivery_message_ids is not None:
        delivery["delivery_message_ids"] = delivery_message_ids
    if not checkpoint_route_step(
        conn, claim.email_id, claim.token, step=step, status=status, **delivery,
    ):
        raise RuntimeError(f"Inbound {step} checkpoint lost for {claim.email_id}")


@dataclass(frozen=True)
class StagedTicketNotice:
    """Presentation prepared without publishing a digest or sending Telegram I/O."""

    notice: object
    card: object | None = None
    text: str = ""
    summary: str = ""


async def prepare_thread_ticket_notice(
    ports: EmailPollPorts, fetched: FetchedEmail, ticket: SupportTicket, *,
    should_send_cards: bool, cards_sent: int, card_batch_limit: int,
    overflow_summaries: list[str],
) -> StagedTicketNotice | None:
    """Only compute the presentation; the final route transaction owns staging."""
    from src.email.ticket_notice import build_ticket_email_notice

    kwargs = dict(
        ticket_id=ticket.ticket_id, sender=fetched.sender_email,
        subject=fetched.subject, body=fetched.body,
        attachment_names=[item.filename for item in fetched.attachments],
    )
    notice = build_ticket_email_notice(**kwargs)
    if not notice.immediate:
        return StagedTicketNotice(notice=notice)
    if not should_send_cards:
        return None
    if cards_sent >= card_batch_limit:
        overflow_summaries.append(
            f"?? {ports.t('sched.email_ticket_title').format(ticket_id=ticket.ticket_id)} ??{fetched.sender_email}"
        )
        return None
    summary = await asyncio.to_thread(
        ports._summarize_email_for_card, fetched.subject,
        fetched.sender_email, fetched.body,
    )
    notice = build_ticket_email_notice(**kwargs, summary=summary)
    card = ports.build_card(
        "TK", notice.title, notice.body, project_id=ticket.related_project_id,
    )
    card.metadata = {"ticket_id": ticket.ticket_id, "email_summary": summary}
    return StagedTicketNotice(
        notice=notice, card=card, text=ports.format_card_text(card), summary=summary,
    )


def stage_thread_ticket_notice(
    conn, claim: InboundRouteClaim, fetched: FetchedEmail,
    ticket: SupportTicket, plan: StagedTicketNotice,
    *, chat_id: int,
) -> None:
    """Stage the digest or card intent in the *same transaction* as finish_route."""
    if plan.card is not None:
        _checkpoint(
            conn, claim, "ticket_notice", "started",
            delivery_intent=RouteDeliveryIntent.for_message(chat_id, plan.text),
        )
        return
    from src.repositories.digest_queue_repo import enqueue_digest_item

    _checkpoint(conn, claim, "ticket_notice", "started")
    enqueue_digest_item(
        conn, category="email_information", title=plan.notice.title,
        body=plan.notice.body, project_id=ticket.related_project_id,
        metadata={"action_required": False, "email_id": fetched.email_id,
                  "ticket_id": ticket.ticket_id},
        commit=False,
    )
    _checkpoint(conn, claim, "ticket_notice", "completed")


def _record_completed_ticket_send(conn, claim: InboundRouteClaim, intent, message_ids):
    """Record local IDs only against the exact completed IMAP intent."""
    if (not isinstance(message_ids, list) or not message_ids
            or any(type(mid) is not int or mid <= 0 for mid in message_ids)):
        raise ValueError("Ticket card requires locally recorded Telegram IDs")
    row = conn.execute(
        "SELECT checkpoint_json FROM inbound_email_work "
        "WHERE email_id=? AND source_type='imap' AND route_state='completed'",
        (claim.email_id,),
    ).fetchone()
    if row is None:
        raise RuntimeError("IMAP ticket receipt is not completed")
    checkpoint = json.loads(row[0])
    delivery = checkpoint.get("route_deliveries", {}).get("ticket_notice", {})
    if (checkpoint.get("route_steps", {}).get("ticket_notice") != "started"
            or delivery.get("state") != "sending"
            or delivery.get("chat_id") != intent.chat_id
            or delivery.get("content_sha256") != intent.content_sha256):
        raise RuntimeError("IMAP ticket card intent changed")
    checkpoint["route_steps"]["ticket_notice"] = "completed"
    delivery.update(state="locally_recorded", message_ids=message_ids)
    updated = conn.execute(
        "UPDATE inbound_email_work SET checkpoint_json=?, updated_at=datetime('now') "
        "WHERE email_id=? AND source_type='imap' AND route_state='completed' "
        "AND checkpoint_json=?",
        (json.dumps(checkpoint, sort_keys=True), claim.email_id, row[0]),
    ).rowcount
    if updated != 1:
        raise RuntimeError("IMAP ticket card receipt changed")


async def send_completed_thread_ticket_notice(
    context, ports: EmailPollPorts, chat_id: int, fetched: FetchedEmail,
    ticket: SupportTicket, claim: InboundRouteClaim, plan: StagedTicketNotice,
) -> None:
    """Never call Telegram until an independent committed receipt is visible."""
    if plan.card is None:
        return
    intent = RouteDeliveryIntent.for_message(chat_id, plan.text)
    with ports.scoped_read() as conn:
        row = conn.execute(
            "SELECT checkpoint_json FROM inbound_email_work "
            "WHERE email_id=? AND source_type='imap' AND route_state='completed'",
            (claim.email_id,),
        ).fetchone()
        if row is None:
            raise RuntimeError("IMAP ticket receipt is not committed")
        checkpoint = json.loads(row[0])
        delivery = checkpoint.get("route_deliveries", {}).get("ticket_notice", {})
        if (checkpoint.get("route_steps", {}).get("ticket_notice") != "started"
                or delivery.get("state") != "sending"
                or delivery.get("chat_id") != intent.chat_id
                or delivery.get("content_sha256") != intent.content_sha256):
            raise RuntimeError("IMAP ticket card intent is not committed")
    message_ids = await ports.send_persisted_message(
        context.bot, chat_id=chat_id, text=plan.text,
        project_id=ticket.related_project_id, message_kind="card",
        reply_markup=ports.card_to_markup(plan.card),
        metadata={"card_profile": "TK", "email_id": fetched.email_id,
                  "email_summary": plan.summary, "ticket_id": ticket.ticket_id},
        require_persistence=True,
    )
    with ports.scoped_write() as conn:
        _record_completed_ticket_send(conn, claim, intent, message_ids)


async def flush_overflow_card(
    context, ports: EmailPollPorts, chat_id: int,
    overflow_summaries: list[str], *, should_send_cards: bool,
) -> None:
    """Claim pending overflow sources before sending, including prior poll cycles.

    The in-memory list is a compatibility argument, not a delivery ledger.
    A claimed batch without local IDs requires human reconciliation; never replay it.
    """
    if not should_send_cards:
        return
    from src.email.inbound_overflow import (
        claim_overflow_batch, pending_overflow_items, record_overflow_result,
    )

    with ports.scoped_write() as conn:
        items = pending_overflow_items(conn, chat_id)
        if not items:
            return
        summaries = [summary for _, summary in items]
        summary_text = ports.t("sched.email_batch_body").format(
            count=len(summaries),
            items="\n".join(f"• {summary}" for summary in summaries[:10]),
        )
        if len(summaries) > 10:
            summary_text += ports.t("sched.overflow_more").format(
                count=len(summaries) - 10,
            )
        card = ports.build_card("B", ports.t("sched.email_batch_title"), summary_text)
        text = ports.format_card_text(card)
        batch_id = claim_overflow_batch(conn, chat_id, items, text)

    message_ids = await ports.send_persisted_message(
        context.bot, chat_id=chat_id, text=text, project_id=None,
        message_kind="card", reply_markup=ports.card_to_markup(card),
        metadata={"card_profile": "B", "overflow_batch_id": batch_id},
        require_persistence=True,
    )
    with ports.scoped_write() as conn:
        record_overflow_result(conn, batch_id, message_ids)
