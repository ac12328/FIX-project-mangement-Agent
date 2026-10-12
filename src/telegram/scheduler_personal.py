"""Generic scheduler tick for personal assistant one-shot reminders."""

from __future__ import annotations

import hashlib
import logging
from datetime import datetime, timezone

from config import defaults
from src.db.connection import scoped_write
from src.repositories.schedule_repo import claim_due_schedules, complete_run
from src.scheduling.service import is_missed_beyond_catchup
from src.services.personal_reminder_delivery import (
    finish_reminder_delivery, mark_reminder_delivery_uncertain,
    prepare_reminder_delivery,
)
from src.telegram.runtime import send_persisted_message

logger = logging.getLogger(__name__)


def _bot_fingerprint(bot) -> str:
    from config.settings import CONFIG

    token = getattr(bot, "token", None)
    if not isinstance(token, str) or not token:
        token = CONFIG.telegram.bot_token
    if not token:
        raise ValueError("Cannot send reminder without a Telegram bot identity")
    return hashlib.sha256(token.encode()).hexdigest()


async def _job_calendar_sync(context) -> None:
    if not (
        defaults.PERSONAL_ASSISTANT_ENABLED
        and defaults.PERSONAL_ASSISTANT_CALENDAR_READ_ENABLED
    ):
        return
    from config.settings import CONFIG

    if not (CONFIG.msgraph.enabled and CONFIG.msgraph.calendar_enabled):
        return
    try:
        from src.scheduling.calendar_sync import sync_calendar_events

        with scoped_write() as conn:
            result = sync_calendar_events(conn)
        logger.info(
            "Calendar sync completed: %d events, %d cancelled",
            result["synced"],
            result["cancelled"],
        )
    except Exception:
        logger.exception("Calendar synchronization failed")
        raise


async def _job_personal_schedules(context) -> None:
    if not (
        defaults.PERSONAL_ASSISTANT_ENABLED
        and defaults.PERSONAL_ASSISTANT_REMINDERS_ENABLED
    ):
        return
    chat_id = context.job.data["chat_id"]
    now = datetime.now(timezone.utc).isoformat()
    # Claim one item and store its send intent in the same transaction. A
    # crash cannot strand later batch-claimed reminders without receipts.
    for _ in range(20):
        try:
            with scoped_write() as conn:
                schedules = claim_due_schedules(conn, now=now, limit=1, commit=False)
                if not schedules:
                    break
                schedule = schedules[0]
                run_id = schedule["run_id"]
                schedule_id = schedule["schedule_id"]
                if is_missed_beyond_catchup(schedule["next_run_at"]):
                    complete_run(
                        conn, run_id, schedule_id, status="missed",
                        result={"reason": "more_than_24_hours_late"}, commit=False,
                    )
                    continue
                text = f"Reminder\n\n{schedule['title']}"
                prepared = prepare_reminder_delivery(
                    conn, run_id=run_id, schedule_id=schedule_id,
                    chat_id=chat_id, bot_fingerprint=_bot_fingerprint(context.bot),
                    text=text,
                )
        except Exception:
            # No network attempt occurred and the claim rolled back. Stop this
            # tick rather than repeatedly picking the same failing due item.
            logger.exception("Personal reminder preparation failed")
            break
        if not prepared:
            continue
        try:
            message_ids = await send_persisted_message(
                context.bot,
                chat_id=chat_id,
                text=text,
                message_kind="reminder",
                operator_id=schedule["operator_id"],
                metadata={"schedule_id": schedule_id, "schedule_run_id": run_id},
                require_persistence=True,
            )
            with scoped_write() as conn:
                finish_reminder_delivery(
                    conn, run_id=run_id, schedule_id=schedule_id,
                    message_ids=message_ids,
                    work_item_id=schedule.get("command_payload", {}).get("work_item_id"),
                )
        except Exception as exc:
            logger.exception(
                "Personal reminder outcome requires manual review for %s", schedule_id,
            )
            try:
                with scoped_write() as conn:
                    mark_reminder_delivery_uncertain(conn, run_id=run_id, error=str(exc))
            except Exception:
                # The committed 'sending' receipt still blocks replay and is
                # visible to manual review after a process restart.
                logger.exception("Could not update reminder receipt for %s", schedule_id)
