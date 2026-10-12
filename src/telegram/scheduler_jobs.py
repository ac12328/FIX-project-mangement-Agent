"""Scheduled job implementations for AgentFIXBPRO Telegram bot."""
from __future__ import annotations

import asyncio  # Facade patch seam shared with extracted email jobs.
import logging

import src.telegram.scheduler as _sched_mod
from src.db.connection import scoped_read, scoped_write
from src.email.inbound_policy import (
    build_email_classifier,
    route_inbound_event,
    should_run_inbound_reply_agent,
)
from src.email.poll_routing import prefer_project_update_routing
from src.telegram.cards import build_card, format_card_text
from src.telegram.i18n import t, button_display, get_ui_language
from src.telegram.markup import card_to_markup
from src.telegram.runtime import send_persisted_message
from src.telegram.scheduler import _mark_job_success, _notify_job_failure
from src.utils.job_retry import with_async_retry
from src.telegram import scheduler_jobs_operational as _operational
from src.telegram import scheduler_jobs_reports as _reports
from src.telegram import scheduler_jobs_email_poll as _email_poll
from src.telegram.scheduler_jobs_email_poll import EmailPollPorts
from src.telegram import scheduler_jobs_actions as _actions
from src.telegram import scheduler_jobs_knowledge as _knowledge
from src.telegram.scheduler_jobs_actions import ActionJobPorts
from src.telegram.scheduler_jobs_knowledge import KnowledgeJobPorts
from src.telegram import scheduler_jobs_outboxes as _outboxes
from src.telegram.scheduler_jobs_outboxes import OutboxJobPorts
from src.telegram.scheduler_jobs_operational import OperationalJobPorts
from src.telegram import scheduler_jobs_inbound_reply as _inbound_reply
from src.telegram.scheduler_jobs_inbound_reply import InboundReplyPorts
from src.telegram import scheduler_jobs_reconciliation as _reconciliation
from src.telegram.scheduler_jobs_reconciliation import ReconciliationPorts

logger = logging.getLogger(__name__)

# Email poll cooperativity — keep Telegram responsive during large inbox backlogs.
_YIELD_EVERY = 5





def _operational_ports() -> OperationalJobPorts:
    """Resolve patchable facade dependencies once for each job invocation."""
    return OperationalJobPorts(
        scoped_read=scoped_read,
        scoped_write=scoped_write,
        send_persisted_message=send_persisted_message,
        _mark_job_success=_mark_job_success,
        _notify_job_failure=_notify_job_failure,
        with_async_retry=with_async_retry,
        build_card=build_card,
        format_card_text=format_card_text,
        t=t,
        button_display=button_display,
        get_ui_language=get_ui_language,
        card_to_markup=card_to_markup,
        logger=logger,
    )


def _email_poll_ports() -> EmailPollPorts:
    """Resolve email poll patch seams for each scheduled invocation."""
    return EmailPollPorts(
        scoped_read=scoped_read,
        scoped_write=scoped_write,
        logger=logger,
        build_email_classifier=build_email_classifier,
        route_inbound_event=route_inbound_event,
        should_run_inbound_reply_agent=should_run_inbound_reply_agent,
        prefer_project_update_routing=prefer_project_update_routing,
        build_card=build_card,
        format_card_text=format_card_text,
        t=t,
        card_to_markup=card_to_markup,
        send_persisted_message=send_persisted_message,
        with_async_retry=with_async_retry,
        _mark_job_success=_mark_job_success,
        _notify_job_failure=_notify_job_failure,
        _YIELD_EVERY=_YIELD_EVERY,
        _summarize_email_for_card=_summarize_email_for_card,
        _process_inbound_reply_via_agent=_process_inbound_reply_via_agent,
    )


def _inbound_reply_ports() -> InboundReplyPorts:
    """Resolve reply processing dependencies at the original facade."""
    return InboundReplyPorts(
        scoped_write=scoped_write,
        send_persisted_message=send_persisted_message,
        build_card=build_card,
        format_card_text=format_card_text,
        card_to_markup=card_to_markup,
        t=t,
        logger=logger,
    )


def _reconciliation_ports() -> ReconciliationPorts:
    """Resolve reconciliation dependencies at the original facade."""
    return ReconciliationPorts(
        scoped_write=scoped_write,
        _mark_job_success=_mark_job_success,
        logger=logger,
    )


def _actions_ports() -> ActionJobPorts:
    """Resolve actions job dependencies for each invocation."""
    return ActionJobPorts(
        scoped_read=scoped_read,
        scoped_write=scoped_write,
        send_persisted_message=send_persisted_message,
        _mark_job_success=_mark_job_success,
        _notify_job_failure=_notify_job_failure,
        with_async_retry=with_async_retry,
        t=t,
        logger=logger,
    )


def _knowledge_ports() -> KnowledgeJobPorts:
    """Resolve knowledge job dependencies for each invocation."""
    return KnowledgeJobPorts(
        scoped_write=scoped_write,
        _mark_job_success=_mark_job_success,
        _notify_job_failure=_notify_job_failure,
        with_async_retry=with_async_retry,
        t=t,
        logger=logger,
    )


def _outbox_ports() -> OutboxJobPorts:
    """Resolve outbox job dependencies for each invocation."""
    return OutboxJobPorts(
        _mark_job_success=_mark_job_success,
        _notify_job_failure=_notify_job_failure,
        with_async_retry=with_async_retry,
        logger=logger,
    )


async def _job_morning_brief(context) -> None:
    """Compatibility entry point for the morning_brief job."""
    await _operational.run_morning_brief(context, _operational_ports())

async def _job_sla_check(context) -> None:
    """Compatibility entry point for the sla_check job."""
    await _operational.run_sla_check(context, _operational_ports())

async def _job_followup_check(context) -> None:
    """Compatibility entry point for the followup_check job."""
    await _operational.run_followup_check(context, _operational_ports())

async def _job_inactivity_check(context) -> None:
    """Compatibility entry point for the inactivity_check job."""
    await _operational.run_inactivity_check(context, _operational_ports())

async def _job_closing_suggestions(context) -> None:
    """Compatibility entry point for the closing_suggestions job."""
    await _operational.run_closing_suggestions(context, _operational_ports())

async def _job_session_timeout(context) -> None:
    """Compatibility entry point for the session_timeout job."""
    await _operational.run_session_timeout(context, _operational_ports())

async def _job_weekly_report(context) -> None:
    """Compatibility entry point for the weekly_report job."""
    await _reports.run_weekly_report(context, _operational_ports())

async def _job_monthly_report(context) -> None:
    """Compatibility entry point for the monthly_report job."""
    await _reports.run_monthly_report(context, _operational_ports())

async def _job_deferred_check(context) -> None:
    """Compatibility entry point for the deferred_check job."""
    await _operational.run_deferred_check(context, _operational_ports())

async def _job_backup_database(context) -> None:
    """Compatibility entry point for the daily backup job."""
    from src.telegram.scheduler_jobs_runtime import run_backup_database

    await run_backup_database(
        context, notify_failure=_notify_job_failure, mark_success=_mark_job_success,
    )


async def _job_bot_heartbeat(context) -> None:
    """Compatibility entry point for the bot heartbeat job."""
    from src.telegram.scheduler_jobs_runtime import run_bot_heartbeat

    await run_bot_heartbeat(context, write_scope=scoped_write)


async def _job_api_health_check(context) -> None:
    """Compatibility entry point for the API health-check job."""
    from src.telegram.scheduler_jobs_runtime import run_api_health_check

    await run_api_health_check(
        context, scheduler_state=_sched_mod, send_message=send_persisted_message,
    )


def _get_project_state(conn, project_id: str) -> dict:
    """Get current project field values for proposal diff display."""
    row = conn.execute(
        "SELECT * FROM projects WHERE project_id = ?", (project_id,)
    ).fetchone()
    return dict(row) if row else {}


def _summarize_email_for_card(subject: str, sender: str, body: str) -> str:
    """Generate an AI summary of an email in the operator's preferred language."""
    from src.llm.client import llm_call
    from config.defaults import EMAIL_BODY_STORE_CHARS
    from src.utils.operator_language import prose_label, read_response_language

    truncated = body[:EMAIL_BODY_STORE_CHARS]
    label = prose_label(read_response_language())
    try:
        return llm_call(
            "triage",
            [
                {"role": "system", "content": (
                    "You are a concise email summariser. "
                    f"Summarise the email below in {label}. "
                    "Include: key request, action items, and any deadlines. "
                    "Keep it under 200 words."
                )},
                {"role": "user", "content": (
                    f"Subject: {subject}\nFrom: {sender}\n\n{truncated}"
                )},
            ],
        )
    except Exception:
        logger.warning("Failed to generate email summary", exc_info=True)
        return ""


async def _process_inbound_reply_via_agent(
    fetched, event, context, chat_id: int, should_send_cards: bool, *, claim=None,
):
    """Compatibility entry point for inbound replies; patch seams resolve per call."""
    return await _inbound_reply.run_inbound_reply_via_agent(
        fetched, event, context, chat_id, should_send_cards, _inbound_reply_ports(),
        claim=claim,
    )


async def _job_email_project_reconciliation(context) -> None:
    """Compatibility entry point for email project reconciliation."""
    await _reconciliation.run_email_project_reconciliation(context, _reconciliation_ports())


async def _job_email_poll(context) -> None:
    """Compatibility entry point for the email polling job."""
    await _email_poll.run_email_poll(context, _email_poll_ports())


async def _job_resolution_followup(context) -> None:
    """Compatibility entry point for resolution_followup."""
    await _actions.run_resolution_followup(context, _actions_ports())


async def _job_support_ticket_aging(context) -> None:
    """Compatibility entry point for support_ticket_aging."""
    await _actions.run_support_ticket_aging(context, _actions_ports())


async def _job_confidence_lifecycle(context) -> None:
    """Compatibility entry point for confidence_lifecycle."""
    await _actions.run_confidence_lifecycle(context, _actions_ports())


async def _job_wiki_curator(context) -> None:
    """Compatibility entry point for wiki_curator."""
    await _knowledge.run_wiki_curator(context, _knowledge_ports())


async def _job_wiki_lint(context) -> None:
    """Compatibility entry point for wiki_lint."""
    await _knowledge.run_wiki_lint(context, _knowledge_ports())


async def _job_insight_mining(context) -> None:
    """Compatibility entry point for insight_mining."""
    await _knowledge.run_insight_mining(context, _knowledge_ports())


async def _job_proactive_drafts(context) -> None:
    """Compatibility entry point for proactive_drafts."""
    await _actions.run_proactive_drafts(context, _actions_ports())


async def _job_draft_expiry(context) -> None:
    """Compatibility entry point for draft_expiry."""
    await _actions.run_draft_expiry(context, _actions_ports())


async def _job_escalation_check(context) -> None:
    """Compatibility entry point for escalation_check."""
    await _actions.run_escalation_check(context, _actions_ports())


async def _job_email_knowledge_mining(context) -> None:
    """Compatibility entry point for email_knowledge_mining."""
    await _knowledge.run_email_knowledge_mining(context, _knowledge_ports())


async def _job_scheduled_sends(context) -> None:
    """Compatibility entry point for scheduled_sends."""
    await _outboxes.run_scheduled_sends(context, _outbox_ports())


async def _job_learning_outbox(context) -> None:
    """Compatibility entry point for learning_outbox."""
    await _outboxes.run_learning_outbox(context, _outbox_ports())


async def _job_knowledge_curation(context) -> None:
    """Compatibility entry point for knowledge_curation."""
    await _knowledge.run_knowledge_curation(context, _knowledge_ports())



async def _job_trace_retention(context) -> None:
    """Compatibility entry point for observability retention."""
    from src.telegram.scheduler_jobs_runtime import run_trace_retention

    await run_trace_retention(context, write_scope=scoped_write)


async def _job_go_live_telegram_outbox(context) -> None:
    """Compatibility entry point for go_live_telegram_outbox."""
    await _outboxes.run_go_live_telegram_outbox(context, _outbox_ports())


async def _job_support_email_effects(context) -> None:
    """Compatibility entry point for committed support email effects."""
    await _outboxes.run_support_email_effects(context, _outbox_ports())


async def _job_email_update_notifications(context) -> None:
    """Compatibility entry point for email_update_notifications."""
    await _outboxes.run_email_update_notifications(context, _outbox_ports())


async def _job_contact_identity_cards(context) -> None:
    """Compatibility entry point for committed identity-card intents."""
    await _outboxes.run_contact_identity_cards(context, _outbox_ports())


async def _job_project_advisories(context) -> None:
    """Compatibility entry point for committed advisory opportunities."""
    await _outboxes.run_project_advisories(context, _outbox_ports())


async def _job_inbound_presentations(context) -> None:
    """Compatibility entry point for committed inbound information cards."""
    await _outboxes.run_inbound_presentations(context, _outbox_ports())


async def _job_commitment_card_outbox(context) -> None:
    """Compatibility entry point for commitment_card_outbox."""
    await _outboxes.run_commitment_card_outbox(context, _outbox_ports())


async def _job_draft_card_outbox(context) -> None:
    """Compatibility entry point for committed operational draft cards."""
    await _outboxes.run_draft_card_outbox(context, _outbox_ports())


async def _job_staged_update_card_outbox(context) -> None:
    """Drain committed project-update confirmation card intents."""
    await _outboxes.run_staged_update_card_outbox(context, _outbox_ports())
