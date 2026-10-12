"""Digest delivery scheduler job — afternoon flush of accumulated notifications."""

from __future__ import annotations

import logging

from src.db.connection import scoped_read, scoped_write
from src.telegram.cards import build_card, format_card_text
from src.telegram.i18n import t
from src.telegram.markup import card_to_markup
from src.telegram.runtime import send_persisted_message
from src.telegram.scheduler import _notify_job_failure, _mark_job_success
from src.utils.push_guard import URGENCY_NORMAL, should_push

logger = logging.getLogger(__name__)


async def _job_afternoon_digest(context) -> None:
    """14:05 HKT digest — flush accumulated non-urgent notifications."""
    data = context.job.data
    chat_id = data["chat_id"]
    try:
        # Honor the operator's notification_mode: a silent-mode operator has
        # suppressed these non-urgent notifications during the day, so do not
        # re-deliver them wholesale at 14:05. Match the guard every other
        # proactive delivery job uses.
        with scoped_read() as conn:
            from src.repositories.operator_state_repo import get_notification_mode
            mode = get_notification_mode(conn)
        if not should_push(mode, URGENCY_NORMAL):
            logger.debug("afternoon_digest suppressed (notification_mode=%s)", mode)
            _mark_job_success("afternoon_digest")
            return

        from src.portfolio.digest_formatter import flush_and_format

        with scoped_write() as conn:
            text, count = flush_and_format(conn)

        if not text:
            logger.info("Afternoon digest: no pending items")
            _mark_job_success("afternoon_digest")
            return

        card = build_card("I", t("sched.afternoon_digest_title"), text, extra_buttons=[["DISMISS"]])
        await send_persisted_message(
            context.bot,
            chat_id=chat_id,
            text=format_card_text(card),
            message_kind="card",
            reply_markup=card_to_markup(card),
        )
        logger.info("Afternoon digest sent (%d items)", count)
        _mark_job_success("afternoon_digest")
    except Exception as exc:
        logger.exception("Failed to send afternoon digest")
        await _notify_job_failure(context, chat_id, "afternoon_digest", exc)
