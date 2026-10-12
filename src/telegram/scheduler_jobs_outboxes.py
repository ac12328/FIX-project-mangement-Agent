"""Scheduled durable outbox dispatchers with preserved retry policy."""
from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any, Callable


@dataclass(frozen=True)
class OutboxJobPorts:
    """Call-time dependencies supplied by the scheduler facade."""
    _mark_job_success: Callable[..., Any]
    _notify_job_failure: Callable[..., Any]
    with_async_retry: Callable[..., Any]
    logger: Any


async def run_scheduled_sends(context, ports: OutboxJobPorts) -> None:
    """Dispatch due durable outbox records through the mandatory Send Gate."""
    data = context.job.data
    chat_id = data["chat_id"]
    try:
        async def _core():
            from src.email.outbox import dispatch_due_outbox

            stats = await asyncio.to_thread(dispatch_due_outbox, limit=10)
            if stats["claimed"]:
                ports.logger.info("Draft outbox dispatch completed: %s", stats)
            return stats

        await ports.with_async_retry(_core, max_retries=1, delay_seconds=5, job_name="scheduled_sends")
        ports._mark_job_success("scheduled_sends")
    except Exception as exc:
        ports.logger.exception("Draft outbox dispatcher job failed")
        await ports._notify_job_failure(context, chat_id, "scheduled_sends", exc)


async def run_learning_outbox(context, ports: OutboxJobPorts) -> None:
    """Drain durable learning jobs without blocking the scheduler event loop."""
    data = context.job.data
    chat_id = data["chat_id"]
    try:
        from src.learning.outbox import process_learning_outbox

        async def _core():
            return await asyncio.to_thread(process_learning_outbox, limit=5)

        result = await ports.with_async_retry(
            _core, max_retries=2, delay_seconds=5, job_name="learning_outbox"
        )
        if result["claimed"]:
            ports.logger.info("Learning outbox: %s", result)
        ports._mark_job_success("learning_outbox")
    except Exception as exc:
        ports.logger.exception("Learning outbox job failed")
        await ports._notify_job_failure(context, chat_id, "learning_outbox", exc)


async def run_go_live_telegram_outbox(context, ports: OutboxJobPorts) -> None:
    """Drain committed M8 notices without replaying uncertain sends."""
    from src.services.go_live_delivery import process_pending_go_live_telegram

    try:
        await asyncio.to_thread(process_pending_go_live_telegram)
        ports._mark_job_success("go_live_telegram_outbox")
    except Exception:
        ports.logger.exception("Go-live Telegram outbox worker failed")


async def run_email_update_notifications(context, ports: OutboxJobPorts) -> None:
    """Retry known-unsent update cards without replaying project mutations."""
    from src.email.update_notification_outbox import process_pending_update_notifications
    try:
        await asyncio.to_thread(process_pending_update_notifications)
        ports._mark_job_success("email_update_notifications")
    except Exception:
        ports.logger.exception("Email update notification outbox worker failed")


async def run_inbound_presentations(context, ports: OutboxJobPorts) -> None:
    """Deliver committed information intents; expired sends need reconciliation."""
    from src.email.inbound_presentation_outbox import process_pending_presentations

    try:
        await asyncio.to_thread(process_pending_presentations)
        ports._mark_job_success("inbound_presentations")
    except Exception:
        ports.logger.exception("Inbound presentation outbox worker failed")


async def run_contact_identity_cards(context, ports: OutboxJobPorts) -> None:
    """Drain committed identity cards; unknown sends require reconciliation."""
    from src.triage.contact_identity_card_outbox import process_pending_contact_identity_cards

    try:
        await asyncio.to_thread(process_pending_contact_identity_cards)
        ports._mark_job_success("contact_identity_cards")
    except Exception:
        ports.logger.exception("Contact identity card outbox worker failed")


async def run_project_advisories(context, ports: OutboxJobPorts) -> None:
    """Evaluate committed opportunities and quarantine unknown Telegram sends."""
    from src.email.project_advisory_outbox import process_pending_project_advisories

    try:
        await asyncio.to_thread(process_pending_project_advisories)
        ports._mark_job_success("project_advisories")
    except Exception:
        ports.logger.exception("Project advisory outbox worker failed")


async def run_support_email_effects(context, ports: OutboxJobPorts) -> None:
    """Drain committed support cards and draft work without holding a write lease."""
    from src.email.support_effects import process_support_effects

    try:
        await asyncio.to_thread(process_support_effects)
        ports._mark_job_success("support_email_effects")
    except Exception:
        ports.logger.exception("Support email effects worker failed")


async def run_commitment_card_outbox(context, ports: OutboxJobPorts) -> None:
    """Drain committed reply cards without replaying uncertain sends."""
    from src.email.commitment_card_outbox import process_pending_commitment_cards

    try:
        await asyncio.to_thread(process_pending_commitment_cards)
        ports._mark_job_success("commitment_card_outbox")
    except Exception:
        ports.logger.exception("Commitment card outbox worker failed")


async def run_draft_card_outbox(context, ports: OutboxJobPorts) -> None:
    """Drain only committed pending drafts; uncertain sends are never replayed."""
    from src.telegram.draft_card_outbox import process_pending_draft_cards

    try:
        await asyncio.to_thread(process_pending_draft_cards)
        ports._mark_job_success("draft_card_outbox")
    except Exception:
        ports.logger.exception("Draft card outbox worker failed")


async def run_staged_update_card_outbox(context, ports: OutboxJobPorts) -> None:
    """Send committed CU cards without holding the stage writer lease."""
    from src.telegram.staged_update_push import process_pending_staged_update_cards

    try:
        await asyncio.to_thread(process_pending_staged_update_cards)
        ports._mark_job_success("staged_update_card_outbox")
    except Exception:
        ports.logger.exception("Staged update card outbox worker failed")
