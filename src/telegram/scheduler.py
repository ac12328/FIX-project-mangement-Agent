"""
Telegram Scheduler for AgentFIXBPRO.
Periodic jobs: morning brief, SLA checks, reports, deferred queue, inbox polling.
"""

from __future__ import annotations

import logging
import sqlite3
from datetime import time, timezone, timedelta

from telegram.ext import Application

from src.db.connection import scoped_read, scoped_write
from src.telegram.runtime import send_persisted_message

logger = logging.getLogger(__name__)

# Jobs where failure should alert the operator via Telegram
_CRITICAL_JOBS = {"email_poll", "sla_check", "morning_brief", "followup_check", "afternoon_digest"}

# Consecutive API health-check failures (reset on success or after alert)
_api_fail_count = 0
_api_seen_healthy = False  # suppress alerts until API has been seen healthy once

# Consecutive failure tracker per job — alert on 1st, then suppress until recovery
_consecutive_failures: dict[str, int] = {}
_ALERT_ON_FAILURE = 1  # alert on the Nth consecutive failure
_RE_ALERT_EVERY = 30   # re-alert every N consecutive failures


async def _notify_job_failure(context, chat_id: int, job_name: str, error: Exception) -> None:
    """Push a Telegram alert when a critical job fails.

    Alerts on the first failure, then suppresses until recovery or every
    ``_RE_ALERT_EVERY`` consecutive failures to avoid spamming the operator.
    """
    count = _consecutive_failures.get(job_name, 0) + 1
    _consecutive_failures[job_name] = count

    # Only alert on the 1st failure or every _RE_ALERT_EVERY after that
    if count != _ALERT_ON_FAILURE and count % _RE_ALERT_EVERY != 0:
        logger.warning(
            "Job %s failed (%d consecutive) — alert suppressed", job_name, count,
        )
        return

    try:
        from src.telegram.error_notices import BACKGROUND_MAINTENANCE, error_notice, next_scheduled_run
        text = error_notice(job_name, type(error).__name__, next_run_at=next_scheduled_run(context, job_name))
        if job_name in BACKGROUND_MAINTENANCE:
            from src.repositories.digest_queue_repo import enqueue_digest_item
            with scoped_write() as conn:
                enqueue_digest_item(conn, category="system_maintenance", title=job_name,
                                    body=text, metadata={"job": job_name}, commit=False)
            return
        await send_persisted_message(
            context.bot, chat_id=chat_id, text=text, message_kind="system",
        )
    except Exception:
        logger.exception("Failed to send job failure alert for %s", job_name)


def _mark_job_success(job_name: str) -> None:
    """Reset the consecutive failure counter for a job after it succeeds."""
    if _consecutive_failures.get(job_name, 0) > 0:
        logger.info("Job %s recovered after %d consecutive failure(s)", job_name, _consecutive_failures[job_name])
        _consecutive_failures[job_name] = 0


# Import job functions after defining what they depend on (avoids circular import)
from src.telegram.scheduler_jobs import (  # noqa: E402
    _job_morning_brief,
    _job_sla_check,
    _job_followup_check,
    _job_inactivity_check,
    _job_closing_suggestions,
    _job_session_timeout,
    _job_weekly_report,
    _job_monthly_report,
    _job_deferred_check,
    _job_backup_database,
    _job_bot_heartbeat,
    _job_api_health_check,
    _job_email_poll,
    _job_email_project_reconciliation,
    _job_resolution_followup,
    _job_support_ticket_aging,
    _job_confidence_lifecycle,
    _job_wiki_curator,
    _job_wiki_lint,
    _job_insight_mining,
    _job_proactive_drafts,
    _job_escalation_check,
    _job_draft_expiry,
    _job_email_knowledge_mining,
    _job_scheduled_sends,
    _job_knowledge_curation,
    _job_learning_outbox,
    _job_trace_retention,
)
from src.telegram.scheduler_jobs_digest import _job_afternoon_digest  # noqa: E402
from src.telegram.scheduler_personal import (  # noqa: E402
    _job_calendar_sync,
    _job_personal_schedules,
)


def register_scheduled_jobs(
    app: Application,
    chat_id: int,
    conn: sqlite3.Connection | None = None,
    *,
    morning_hour: int = 9,
    morning_minute: int = 0,
) -> None:
    """Register all periodic jobs on the Application's JobQueue."""
    from datetime import datetime

    jq = app.job_queue
    if jq is None:
        logger.warning("JobQueue not available - scheduled jobs disabled")
        return

    hkt = timezone(timedelta(hours=8))
    jq.run_daily(
        _job_morning_brief,
        time=time(hour=morning_hour, minute=morning_minute, tzinfo=hkt),
        data={"chat_id": chat_id},
        name="morning_brief",
        days=(0, 1, 2, 3, 4),  # weekdays only (Mon=0 … Fri=4)
    )
    from src.telegram.scheduler_jobs import _job_email_update_notifications
    jq.run_repeating(_job_email_update_notifications, interval=60, first=60,
        data={"chat_id": chat_id}, name="email_update_notifications")
    from src.telegram.scheduler_jobs import _job_inbound_presentations
    jq.run_repeating(_job_inbound_presentations, interval=60, first=60,
        data={"chat_id": chat_id}, name="inbound_presentations")
    from src.telegram.scheduler_jobs import _job_contact_identity_cards
    jq.run_repeating(_job_contact_identity_cards, interval=60, first=60,
        data={"chat_id": chat_id}, name="contact_identity_cards")
    from src.telegram.scheduler_jobs import _job_project_advisories
    jq.run_repeating(_job_project_advisories, interval=60, first=60,
        data={"chat_id": chat_id}, name="project_advisories")
    from src.telegram.scheduler_jobs import _job_commitment_card_outbox
    jq.run_repeating(_job_commitment_card_outbox, interval=60, first=60,
        data={"chat_id": chat_id}, name="commitment_card_outbox")
    from src.telegram.scheduler_jobs import _job_draft_card_outbox
    jq.run_repeating(_job_draft_card_outbox, interval=60, first=60,
        data={"chat_id": chat_id}, name="draft_card_outbox")
    from src.telegram.scheduler_jobs import _job_staged_update_card_outbox
    jq.run_repeating(_job_staged_update_card_outbox, interval=60, first=60,
        data={"chat_id": chat_id}, name="staged_update_card_outbox")
    from src.telegram.scheduler_jobs import _job_go_live_telegram_outbox
    jq.run_repeating(_job_go_live_telegram_outbox, interval=60, first=60,
        data={"chat_id": chat_id}, name="go_live_telegram_outbox")
    from src.telegram.scheduler_jobs import _job_support_email_effects
    jq.run_repeating(_job_support_email_effects, interval=60, first=60,
        data={"chat_id": chat_id}, name="support_email_effects")
    jq.run_repeating(_job_sla_check, interval=14400, first=120, data={"chat_id": chat_id}, name="sla_check")
    jq.run_repeating(_job_followup_check, interval=7200, first=120, data={"chat_id": chat_id}, name="followup_check")
    jq.run_daily(_job_inactivity_check, time=time(hour=10, minute=0, tzinfo=hkt), data={"chat_id": chat_id}, name="inactivity_check")
    jq.run_repeating(_job_session_timeout, interval=300, first=180, data={"chat_id": chat_id}, name="session_timeout")
    jq.run_daily(_job_weekly_report, time=time(hour=17, minute=0, tzinfo=hkt), days=(4,), data={"chat_id": chat_id}, name="weekly_report")
    jq.run_monthly(_job_monthly_report, when=time(hour=9, minute=0, tzinfo=hkt), day=1, data={"chat_id": chat_id}, name="monthly_report")
    jq.run_daily(_job_deferred_check, time=time(hour=11, minute=0, tzinfo=hkt), data={"chat_id": chat_id}, name="deferred_check")
    jq.run_daily(
        _job_closing_suggestions,
        time=time(hour=17, minute=30, tzinfo=hkt),
        data={"chat_id": chat_id},
        name="closing_suggestions",
        days=(0, 1, 2, 3, 4),  # weekdays only
    )
    jq.run_daily(
        _job_trace_retention,
        time=time(hour=3, minute=30, tzinfo=hkt),
        data={"chat_id": chat_id},
        name="trace_retention",
    )
    jq.run_daily(
        _job_backup_database,
        time=time(hour=3, minute=0, tzinfo=hkt),
        data={"chat_id": chat_id},
        name="backup_database",
    )
    jq.run_repeating(
        _job_bot_heartbeat, interval=300, first=120,
        data={"chat_id": chat_id}, name="bot_heartbeat",
    )
    jq.run_repeating(
        _job_api_health_check, interval=300, first=120,
        data={"chat_id": chat_id}, name="api_health_check",
    )
    jq.run_repeating(
        _job_resolution_followup, interval=900, first=300,
        data={"chat_id": chat_id}, name="resolution_followup",
    )
    jq.run_repeating(
        _job_support_ticket_aging, interval=14400, first=600,
        data={"chat_id": chat_id}, name="support_ticket_aging",
    )
    jq.run_daily(
        _job_confidence_lifecycle,
        time=time(hour=19, minute=0, tzinfo=timezone.utc),
        days=(5,),  # Saturday UTC = Sunday 3:00 AM HKT
        data={"chat_id": chat_id},
        name="confidence_lifecycle",
    )
    jq.run_daily(
        _job_wiki_curator,
        time=time(hour=18, minute=0, tzinfo=timezone.utc),  # 2:00 AM HKT = 18:00 UTC
        data={"chat_id": chat_id},
        name="wiki_curator",
    )
    jq.run_daily(
        _job_wiki_lint,
        time=time(hour=19, minute=0, tzinfo=timezone.utc),  # Sunday 3AM HKT
        days=(6,),  # Saturday UTC = Sunday HKT
        data={"chat_id": chat_id},
        name="wiki_lint",
    )
    jq.run_daily(
        _job_insight_mining,
        time=time(hour=23, minute=0, tzinfo=timezone.utc),  # Sunday 7AM HKT
        days=(6,),  # Saturday UTC = Sunday HKT
        data={"chat_id": chat_id},
        name="insight_mining",
    )
    jq.run_daily(
        _job_proactive_drafts,
        time=time(hour=11, minute=30, tzinfo=hkt),
        data={"chat_id": chat_id},
        name="proactive_drafts",
        days=(0, 1, 2, 3, 4),  # weekdays only
    )
    jq.run_daily(
        _job_email_project_reconciliation,
        time=time(hour=11, minute=45, tzinfo=hkt),
        data={"chat_id": chat_id},
        name="email_project_reconciliation",
        days=(0, 1, 2, 3, 4),  # weekdays only
    )
    jq.run_daily(
        _job_escalation_check,
        time=time(hour=14, minute=0, tzinfo=hkt),
        data={"chat_id": chat_id},
        name="escalation_check",
        days=(0, 1, 2, 3, 4),  # weekdays only
    )
    jq.run_daily(
        _job_afternoon_digest,
        time=time(hour=14, minute=5, tzinfo=hkt),
        data={"chat_id": chat_id},
        name="afternoon_digest",
        days=(0, 1, 2, 3, 4),  # weekdays only
    )
    jq.run_daily(
        _job_draft_expiry,
        time=time(hour=2, minute=0, tzinfo=hkt),
        data={"chat_id": chat_id},
        name="draft_expiry",
    )

    jq.run_daily(
        _job_email_knowledge_mining,
        time=time(hour=7, minute=0, tzinfo=hkt),  # 7:00 AM HKT
        data={"chat_id": chat_id},
        name="email_knowledge_mining",
    )

    jq.run_repeating(
        _job_scheduled_sends, interval=5, first=5,
        data={"chat_id": chat_id}, name="scheduled_sends",
    )
    jq.run_repeating(
        _job_learning_outbox, interval=60, first=45,
        data={"chat_id": chat_id}, name="learning_outbox",
    )

    from config.settings import CONFIG as cfg

    if cfg.personal_assistant.enabled and cfg.personal_assistant.reminders_enabled:
        jq.run_repeating(
            _job_personal_schedules,
            interval=60,
            first=45,
            data={"chat_id": chat_id},
            name="personal_schedules",
        )
    if (
        cfg.personal_assistant.enabled is True
        and cfg.personal_assistant.calendar_read_enabled is True
        and cfg.msgraph.enabled is True
        and cfg.msgraph.calendar_enabled is True
    ):
        jq.run_repeating(
            _job_calendar_sync,
            interval=max(60, cfg.msgraph.calendar_sync_interval_seconds),
            first=60,
            data={"chat_id": chat_id},
            name="calendar_sync",
        )

    jq.run_daily(
        _job_knowledge_curation,
        time=time(hour=0, minute=0, tzinfo=timezone.utc),  # Sunday 08:00 HKT = Sunday 00:00 UTC
        days=(6,),  # Sunday UTC
        data={"chat_id": chat_id},
        name="knowledge_curation",
    )

    if cfg.email.imap.enabled:
        jq.run_repeating(
            _job_email_poll,
            interval=cfg.email.imap.poll_interval_seconds,
            first=30,
            data={"chat_id": chat_id},
            name="email_poll",
        )

    # Startup catchup: if bot missed the morning brief (started after 9am HKT on a weekday),
    # fire it once 30 seconds after startup so the operator isn't left without a brief.
    now_hkt = datetime.now(hkt)
    brief_cutoff = now_hkt.replace(
        hour=morning_hour, minute=morning_minute, second=0, microsecond=0
    )
    end_of_window = now_hkt.replace(hour=17, minute=0, second=0, microsecond=0)
    if now_hkt.weekday() < 5 and brief_cutoff < now_hkt < end_of_window:
        # Check if morning brief was already sent today (dedup across restarts).
        # The card title is the i18n string (e.g. "晨間簡報"), NOT "Morning Brief",
        # so match against the localized title that is actually persisted.
        def _brief_already_sent(c, title_pat: str, today_start: str) -> bool:
            row = c.execute(
                "SELECT 1 FROM telegram_messages "
                "WHERE message_kind = 'card' AND text LIKE ? "
                "AND created_at >= ? LIMIT 1",
                (title_pat, today_start),
            ).fetchone()
            return row is not None

        _already_sent = False
        try:
            from src.telegram.i18n import t as _t
            brief_title = _t("sched.morning_brief_title")
            title_pat = f"%{brief_title}%"
            today_start = brief_cutoff.astimezone(timezone.utc).isoformat()
            if conn is not None:
                _already_sent = _brief_already_sent(conn, title_pat, today_start)
            else:
                with scoped_read() as rconn:
                    _already_sent = _brief_already_sent(rconn, title_pat, today_start)
        except Exception:
            pass  # on error, let it fire (better to double-send than miss)

        if _already_sent:
            logger.info("Morning brief already sent today — skipping catchup")
        else:
            logger.info(
                "Bot started after morning brief window (%02d:%02d HKT) — scheduling catchup in 90s",
                morning_hour, morning_minute,
            )
            jq.run_once(
                _job_morning_brief, when=90,
                data={"chat_id": chat_id}, name="morning_brief_catchup",
            )
