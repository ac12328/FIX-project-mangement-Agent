"""Inbound reply agent processing behind the scheduler compatibility facade."""
from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Callable

if TYPE_CHECKING:
    from src.email.inbound_work import RouteDeliveryIntent
    from src.telegram.email_poll_delivery import InboundRouteClaim


def _field_change_outcome_line(write_action: str, changes_text: str) -> str | None:
    """Describe proposed changes according to the actual persistence outcome."""
    if write_action in {
        "auto_applied_undo_card", "direct_write", "auto_applied_notification_pending",
        "auto_applied_notification_unknown", "pct_completed",
    }:
        return f"\n? Auto-applied: {changes_text}"
    if write_action == "below_review_threshold":
        return f"\n?? Suggested changes not queued: {changes_text}"
    if write_action == "review_persistence_failed":
        return f"\n?? Review proposal was not queued; no project fields were changed: {changes_text}"
    return None


class InboundReplyRouteIncomplete(RuntimeError):
    """The reply was recorded, but a field command was not persisted."""


@dataclass(frozen=True)
class InboundReplyPorts:
    """Call-time scheduler dependencies; keep existing test and runtime patch seams."""
    scoped_write: Callable[..., Any]
    send_persisted_message: Callable[..., Any]
    build_card: Callable[..., Any]
    format_card_text: Callable[..., Any]
    card_to_markup: Callable[..., Any]
    t: Callable[..., Any]
    logger: Any


def _checkpoint_reply_card(
    ports: InboundReplyPorts, claim: InboundRouteClaim, status: str, *,
    intent: RouteDeliveryIntent | None = None, message_ids: list[int] | None = None,
) -> None:
    from src.email.inbound_work import checkpoint_route_step

    with ports.scoped_write() as conn:
        if not checkpoint_route_step(
            conn, claim.email_id, claim.token, step="reply_card", status=status,
            delivery_intent=intent, delivery_message_ids=message_ids,
        ):
            raise RuntimeError(f"Inbound reply_card checkpoint lost for {claim.email_id}")


async def run_inbound_reply_via_agent(
    fetched, event, context, chat_id: int, should_send_cards: bool, ports: InboundReplyPorts,
    *, claim: InboundRouteClaim | None = None,
):
    """Process inbound reply using unified agent_loop, replacing standalone LLM calls.

    Returns (InboundReply, proposed_fields) for compatibility with downstream code.
    """
    if claim is not None and claim.email_id != fetched.email_id:
        raise ValueError("Inbound reply claim does not match source email")
    scoped_write = ports.scoped_write
    send_persisted_message = ports.send_persisted_message
    build_card = ports.build_card
    format_card_text = ports.format_card_text
    card_to_markup = ports.card_to_markup
    t = ports.t
    logger = ports.logger
    from config.defaults import EMAIL_BODY_STORE_CHARS
    from src.email.inbound_policy import EMAIL_BACKGROUND_CHAT_ID
    from src.models.inbound_reply import InboundReply
    from src.repositories.activity_log_repo import insert_activity
    from src.repositories.inbound_reply_repo import insert_inbound_reply, make_reply_id
    from src.email.agent_entry import inbound_agent_reply, run_inbound_email_agent
    from src.tools.chat_handlers import clear_email_analysis_result, get_email_analysis_result

    from src.email.email_envelope import build_inbound_analysis_text

    clear_email_analysis_result(
        project_id=event.project_id,
        analysis_ref=fetched.email_id,
    )
    email_text = build_inbound_analysis_text(
        project_id=event.project_id,
        sender_email=fetched.sender_email,
        subject=fetched.subject or "(no subject)",
        body=(fetched.body or "")[:EMAIL_BODY_STORE_CHARS],
        fetched=fetched,
        tag="[INBOUND REPLY]",
    )

    agent_result = await asyncio.to_thread(
        run_inbound_email_agent,
        None,
        email_text,
        EMAIL_BACKGROUND_CHAT_ID,
        max_rounds=5,
    )
    # Read THIS project's result so a concurrent chat analyzing a different
    # project cannot overwrite the scheduler's in-flight analysis.
    analysis = get_email_analysis_result(
        project_id=event.project_id,
        analysis_ref=fetched.email_id,
    )

    if analysis:
        summary = analysis.get("summary", "")
        suggested_action = analysis.get("suggested_action", "")
        confidence = analysis.get("confidence", 0.7)
        proposed_fields = analysis.get("proposed_fields") or {}
        email_intent = analysis.get("email_intent", "")
    else:
        summary = inbound_agent_reply(agent_result)[:500]
        suggested_action = ""
        confidence = 0.5
        proposed_fields = {}
        email_intent = ""

    from config.defaults import EMAIL_BODY_SNIPPET_CHARS
    from src.email.analysis_guards import adjust_email_analysis_result

    confidence, proposed_fields = adjust_email_analysis_result(
        (fetched.body or "")[:EMAIL_BODY_SNIPPET_CHARS],
        confidence,
        proposed_fields,
        sender_email=fetched.sender_email,
        email_intent=email_intent,
        has_attachments=bool(getattr(fetched, "attachments", None)),
    )

    from src.db.connection import scoped_read
    from src.email.project_state_assessment import apply_email_project_state_policy

    with scoped_read() as read_conn:
        state_result = apply_email_project_state_policy(
            read_conn,
            project_id=event.project_id,
            proposed_fields=proposed_fields,
            assessment_detail=(analysis or {}).get("assessment_detail"),
            source_ref=fetched.email_id,
            sender_email=fetched.sender_email,
            received_at=fetched.received_at,
            body=fetched.body or "",
            attachments=getattr(fetched, "attachments", None),
        )
    proposed_fields = state_result.proposed_fields

    reply_id = make_reply_id()
    reply = InboundReply(
        reply_id=reply_id,
        thread_id=fetched.in_reply_to,
        project_id=event.project_id,
        from_contact_id=None,
        received_at=fetched.received_at,
        subject=fetched.subject,
        body_text=(fetched.body or "")[:EMAIL_BODY_STORE_CHARS],
        original_draft_id=None,
        summary=summary,
        suggested_action=suggested_action,
        confidence=confidence,
    )

    post_commit: list[Callable[[], None]] = []
    if claim is not None:
        from src.email.inbound_work import checkpoint_route_step

        with scoped_write() as conn:
            if not checkpoint_route_step(
                conn, claim.email_id, claim.token,
                step="reply_persistence", status="started",
            ):
                raise RuntimeError(f"Inbound reply_persistence checkpoint lost for {claim.email_id}")
    with scoped_write() as conn:
        insert_inbound_reply(conn, reply)
        insert_activity(
            conn,
            actor="email_agent",
            action_type="inbound_reply",
            project_id=event.project_id,
            surface="email",
            summary=f"Client reply from {fetched.sender_email}: {fetched.subject or '(no subject)'}",
        )
        write_action = "skipped"
        if proposed_fields:
            from src.email.inbound_reply_field_route import route_reply_fields

            write_action = route_reply_fields(
                conn,
                project_id=event.project_id,
                proposed_fields=proposed_fields,
                confidence=confidence,
                evidence_ref=f"inbound_reply:{reply_id}",
                actor="email_agent",
                reason=f"Inbound reply from {fetched.sender_email}",
                summary=summary,
                suggested_action=suggested_action,
                sender_email=fetched.sender_email or "",
                email_body=(fetched.body or "")[:EMAIL_BODY_SNIPPET_CHARS],
                email_intent=email_intent or "",
                has_attachments=bool(getattr(fetched, "attachments", None)),
                subject=fetched.subject,
                assessment=(
                    state_result.assessment.json_payload()
                    if state_result.assessment is not None else None
                ),
                policy_version=(
                    state_result.assessment.policy_version
                    if state_result.assessment is not None else None
                ),
                source_type=(
                    state_result.assessment.source_type.value
                    if state_result.assessment is not None else None
                ),
                source_ref=fetched.email_id,
                post_commit=post_commit,
            )
        if claim is not None:
            from src.email.inbound_work import ReplyCommandReceipt

            if not checkpoint_route_step(
                conn, claim.email_id, claim.token,
                step="reply_persistence",
                status="incomplete" if write_action in {"review_persistence_failed", "field_route_failed"} else "completed",
                command_receipt=ReplyCommandReceipt(
                    reply_id=reply_id, write_action=write_action,
                ),
            ):
                raise RuntimeError(f"Inbound reply_persistence receipt lost for {claim.email_id}")

    # A successful scope has committed and released the serializer lease.
    for callback in post_commit:
        try:
            callback()
        except Exception:
            logger.exception("Post-commit inbound reply callback failed for %s", event.project_id)

    if write_action in {"review_persistence_failed", "field_route_failed"}:
        # The reply and any earlier sibling commands have already committed.
        # Do not certify the route or imply that retrying the whole email is safe.
        failure = "review" if write_action == "review_persistence_failed" else "field decision"
        raise InboundReplyRouteIncomplete(
            f"Inbound reply {failure} was not persisted for {fetched.email_id}"
        )

    if should_send_cards and write_action not in {
        "review_card", "review_notification_pending", "review_notification_unknown",
    }:
        title = t("sched.client_reply_title")
        body_parts = [t("sched.client_reply_from").format(sender=fetched.sender_email)]
        if fetched.subject:
            body_parts.append(t("sched.client_reply_re").format(subject=fetched.subject))
        if summary:
            body_parts.append(f"\n{t('sched.client_reply_summary').format(summary=summary)}")
        if suggested_action:
            body_parts.append(t("sched.client_reply_suggested").format(action=suggested_action))
        if proposed_fields:
            changes_text = ", ".join(f"{k} ??{v}" for k, v in proposed_fields.items())
            outcome_line = _field_change_outcome_line(write_action, changes_text)
            if outcome_line:
                body_parts.append(outcome_line)

        if write_action == "pct_completed":
            from src.email.pct_completion_routing import completion_summary
            with scoped_read() as completed_conn:
                body_parts.append(completion_summary(completed_conn, event.project_id))
        body_text = "\n".join(body_parts)
        if write_action in ("auto_applied_undo_card", "direct_write", "auto_applied_notification_pending", "auto_applied_notification_unknown"):
            extra_buttons = [[
                ("UNDO", f"L:UNDO:{event.project_id}"),
                ("Dismiss", f"L:DISMISS:{event.project_id}"),
            ], [
                ("?? Attach to Ticket", f"ATTACH:PICK:{reply_id}"),
            ]]
        else:
            extra_buttons = [[
                ("Dismiss", f"L:DISMISS:{event.project_id}"),
            ], [
                ("?? Attach to Ticket", f"ATTACH:PICK:{reply_id}"),
            ]]
        card_l = build_card("L", title, body_text, project_id=event.project_id, extra_buttons=extra_buttons)
        card_text = format_card_text(card_l)
        if claim is not None:
            from src.email.inbound_work import RouteDeliveryIntent

            _checkpoint_reply_card(
                ports, claim, "started",
                intent=RouteDeliveryIntent.for_message(chat_id, card_text),
            )
        message_ids = await send_persisted_message(
            context.bot,
            chat_id=chat_id,
            text=card_text,
            project_id=event.project_id,
            message_kind="card",
            reply_markup=card_to_markup(card_l),
            object_links=[{"object_type": "inbound_reply", "object_ref": reply_id, "project_id": event.project_id}],
            **({"require_persistence": True} if claim is not None else {}),
        )
        if claim is not None:
            _checkpoint_reply_card(ports, claim, "completed", message_ids=message_ids)
        logger.info(
            "Inbound reply card pushed for project %s (write_action=%s)",
            event.project_id,
            write_action,
        )
        try:
            from src.portfolio.draft_engine import generate_draft_for_signal
            draft_hints = {
                "reply_body": fetched.body[:800] if fetched.body else "",
                "reply_subject": fetched.subject or "",
                "sender_email": fetched.sender_email or "",
                "requested_docs": reply.requested_docs,
            }
            generate_draft_for_signal(
                None,
                signal_type="inbound_reply",
                project_id=event.project_id,
                context_hints=draft_hints,
                chat_id=chat_id,
            )
        except Exception:
            logger.exception("Failed to auto-generate reply draft for %s", event.project_id)

    return reply, proposed_fields
