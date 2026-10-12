"""Operational reminder jobs with explicit runtime ports and unchanged policy."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable


@dataclass(frozen=True)
class OperationalJobPorts:
    """Call-time dependencies preserve the scheduler facade's patch seams."""
    scoped_read: Callable[..., Any]
    scoped_write: Callable[..., Any]
    send_persisted_message: Callable[..., Any]
    _mark_job_success: Callable[..., Any]
    _notify_job_failure: Callable[..., Any]
    with_async_retry: Callable[..., Any]
    build_card: Callable[..., Any]
    format_card_text: Callable[..., Any]
    t: Callable[..., Any]
    button_display: Callable[..., Any]
    get_ui_language: Callable[..., Any]
    card_to_markup: Callable[..., Any]
    logger: Any


async def run_morning_brief(context, ports: OperationalJobPorts) -> None:
    data = context.job.data
    chat_id = data["chat_id"]
    try:
        from src.repositories.operator_state_repo import get_notification_mode
        from src.utils.push_guard import URGENCY_NORMAL, should_push
        with ports.scoped_read() as conn:
            if not should_push(get_notification_mode(conn), URGENCY_NORMAL):
                ports.logger.debug("Morning brief suppressed (notification_mode=%s)", get_notification_mode(conn))
                return
        async def _core():
            from datetime import datetime, timezone
            from src.utils.calendar import is_business_day
            from src.portfolio.morning_brief import format_brief_with_llm, generate_morning_brief
            from src.repositories.working_memory_repo import get_memory

            if not is_business_day(datetime.now(timezone.utc)):
                ports.logger.info("Morning brief skipped — not a business day")
                return

            # Check snooze
            with ports.scoped_read() as conn:
                snooze_mem = get_memory(conn, "_system", "brief_snoozed_until")
            if snooze_mem:
                try:
                    from datetime import datetime as _dt
                    snoozed_until = _dt.fromisoformat(snooze_mem["memory_value"])
                    if _dt.now(timezone.utc) < snoozed_until:
                        ports.logger.info("Morning brief skipped — snoozed until %s", snoozed_until.date())
                        return
                except Exception:
                    pass

            from config import defaults

            with ports.scoped_read() as conn:
                brief = generate_morning_brief(conn)
            if (
                defaults.PERSONAL_ASSISTANT_ENABLED
                and defaults.PERSONAL_ASSISTANT_PERSONAL_BRIEF_ENABLED
            ):
                from config.settings import CONFIG
                from src.assistant.brief.builder import (
                    build_personal_brief,
                    format_personal_brief,
                    save_personal_brief_snapshot,
                )

                with ports.scoped_read() as conn:
                    personal_payload = build_personal_brief(conn, CONFIG.operator_id)
                with ports.scoped_write() as conn:
                    save_personal_brief_snapshot(conn, personal_payload, commit=False)
                brief_text = format_personal_brief(personal_payload)
            else:
                brief_text = format_brief_with_llm(brief)
            # Flush overnight digest items into morning brief
            from src.portfolio.digest_formatter import flush_and_format
            with ports.scoped_write() as wconn:
                digest_text, digest_count = flush_and_format(wconn)
            if digest_text:
                brief_text += f"\n\n{'━' * 23}\n📋 待處理通知摘要 ({digest_count})\n{digest_text}"
            # D1: Append suppressed notification digest
            from src.utils.push_guard import reset_suppressed_count
            suppressed = reset_suppressed_count()
            if suppressed > 0:
                brief_text += ports.t("sched.brief_suppressed").format(count=suppressed)
            card = ports.build_card("I", ports.t("sched.morning_brief_title"), brief_text)
            await ports.send_persisted_message(context.bot, chat_id=chat_id, text=ports.format_card_text(card), message_kind="card", reply_markup=ports.card_to_markup(card))
            ports.logger.info("Morning brief sent (%d active projects)", brief.total_active)

        await ports.with_async_retry(_core, max_retries=1, delay_seconds=60, job_name="morning_brief")
        ports._mark_job_success("morning_brief")
    except Exception as exc:
        ports.logger.exception("Failed to send morning brief")
        await ports._notify_job_failure(context, chat_id, "morning_brief", exc)



async def run_sla_check(context, ports: OperationalJobPorts) -> None:
    data = context.job.data
    chat_id = data["chat_id"]
    try:
        from config.settings import CONFIG
        from src.repositories.operator_state_repo import get_notification_mode
        from src.utils.push_guard import URGENCY_URGENT
        from src.utils.working_hours import should_notify
        with ports.scoped_read() as conn:
            if not should_notify(CONFIG, get_notification_mode(conn), URGENCY_URGENT):
                ports.logger.debug("SLA check suppressed (notification_mode=%s)", get_notification_mode(conn))
                return
        async def _core():
            from datetime import datetime, timezone
            from src.portfolio.sla_monitor import run_sla_check

            with ports.scoped_read() as conn:
                report = run_sla_check(conn)
            urgent = report.overdue + report.due_today + report.urgent
            if not urgent:
                return
            today_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
            for alert in urgent:
                # Daily dedup: skip if already alerted today (F8)
                dedup_key = f"sla_alert_{alert.project_id}_{alert.alert_type}"
                with ports.scoped_read() as rconn:
                    from src.repositories.working_memory_repo import get_memory
                    mem = get_memory(rconn, "_system", dedup_key)
                    if mem and mem.get("memory_value") == today_str:
                        continue
                level_label = alert.alert_level.upper().replace("_", " ")
                level_emoji = {"OVERDUE": "\U0001f534", "DUE_TODAY": "\U0001f7e1", "URGENT": "\U0001f7e0"}.get(alert.alert_level.upper().replace(" ", "_"), "\u26aa")
                body = ports.t("sched.sla_body").format(
                    description=alert.description,
                    emoji=level_emoji,
                    level=level_label,
                    due_date=alert.due_date,
                )
                # Accumulate to digest instead of pushing individual card
                from datetime import date as _date
                from src.repositories.digest_queue_repo import enqueue_digest_item
                try:
                    _due = _date.fromisoformat(alert.due_date)
                    _days_over = (_date.today() - _due).days
                    _due_label = f"已逾期 {_days_over} 天" if _days_over > 0 else f"Due: {alert.due_date}"
                except (ValueError, TypeError):
                    _due_label = f"Due: {alert.due_date}"
                with ports.scoped_write() as conn:
                    from src.repositories.working_memory_repo import upsert_memory
                    upsert_memory(conn, "_system", dedup_key, today_str)
                    enqueue_digest_item(
                        conn,
                        category="sla_alert",
                        title=f"{level_emoji} {alert.project_id}",
                        body=body + "\n" + _due_label + ("\nConfirm progress or agree a revised deadline; no date was changed by this alert." if ports.get_ui_language() == "en" else "\n請確認進度或協調新期限；此提醒沒有自行修改期限。"),
                        project_id=alert.project_id,
                        metadata={"alert_type": alert.alert_type, "alert_level": alert.alert_level},
                        commit=False,
                    )

                # Escalation: track first-alert time and escalate if threshold exceeded
                if CONFIG.escalation_contacts:
                    now = datetime.now(timezone.utc)
                    esc_key = f"sla_escalation_first_{alert.project_id}_{alert.alert_type}"
                    with ports.scoped_read() as rconn2:
                        esc_mem = get_memory(rconn2, "_system", esc_key)
                    if not esc_mem or not esc_mem.get("memory_value"):
                        # First alert — store timestamp
                        with ports.scoped_write() as wconn2:
                            upsert_memory(wconn2, "_system", esc_key, now.isoformat())
                    else:
                        # Subsequent alert — check if escalation threshold exceeded
                        try:
                            first_alert_time = datetime.fromisoformat(esc_mem["memory_value"])
                            if first_alert_time.tzinfo is None:
                                first_alert_time = first_alert_time.replace(tzinfo=timezone.utc)
                            elapsed_seconds = (now - first_alert_time).total_seconds()
                            if elapsed_seconds > CONFIG.escalation_after_hours * 3600:
                                esc_body = (
                                    f"\u2b06\ufe0f ESCALATED \u2014 SLA breach persists over "
                                    f"{CONFIG.escalation_after_hours}h\n\n{body}"
                                )
                                esc_card = ports.build_card("M", ports.t("sched.sla_title"), esc_body, project_id=alert.project_id)
                                for esc_chat_id in CONFIG.escalation_contacts:
                                    await ports.send_persisted_message(
                                        context.bot,
                                        chat_id=esc_chat_id,
                                        text=ports.format_card_text(esc_card),
                                        project_id=alert.project_id,
                                        message_kind="card",
                                        reply_markup=ports.card_to_markup(esc_card),
                                    )
                                ports.logger.info(
                                    "SLA escalation sent for %s to %d contacts",
                                    alert.project_id, len(CONFIG.escalation_contacts),
                                )
                        except (ValueError, TypeError):
                            ports.logger.warning("Invalid escalation timestamp for %s", esc_key)

            ports.logger.info("SLA alert pushed: %d urgent items", len(urgent))

        await ports.with_async_retry(_core, max_retries=1, delay_seconds=60, job_name="sla_check")
        ports._mark_job_success("sla_check")
    except Exception as exc:
        ports.logger.exception("Failed to run SLA check")
        await ports._notify_job_failure(context, chat_id, "sla_check", exc)



async def run_followup_check(context, ports: OperationalJobPorts) -> None:
    data = context.job.data
    chat_id = data["chat_id"]
    try:
        from config.settings import CONFIG
        from src.repositories.operator_state_repo import get_notification_mode
        from src.utils.push_guard import URGENCY_NORMAL
        from src.utils.working_hours import should_notify
        with ports.scoped_read() as conn:
            if not should_notify(CONFIG, get_notification_mode(conn), URGENCY_NORMAL):
                ports.logger.debug("Follow-up check suppressed (notification_mode=%s)", get_notification_mode(conn))
                return
        async def _core():
            from src.portfolio.followup_tracker import check_escalations

            with ports.scoped_write() as conn:
                actions = check_escalations(conn)
            actionable = [a for a in actions if a.action in ("nudge", "escalate", "overdue")]
            if not actionable:
                return
            from src.repositories.digest_queue_repo import enqueue_digest_item
            for action in actionable:
                overdue_tag = f" (逾期 {action.days_overdue} 天)" if action.days_overdue > 0 else ""
                from src.telegram.reminder_notices import followup_reminder
                from src.portfolio.followup_tracker import get_followup
                with ports.scoped_read() as rconn:
                    record = get_followup(rconn, action.followup_id) or {"description": action.description}
                    _followup_body = followup_reminder(rconn, record, days_overdue=action.days_overdue)
                with ports.scoped_write() as conn:
                    enqueue_digest_item(
                        conn,
                        category="followup_overdue",
                        title=f"{action.action.title()} — {action.project_id}",
                        body=_followup_body,
                        project_id=action.project_id,
                        metadata={"followup_id": action.followup_id, "action": action.action},
                        commit=False,
                    )
            ports.logger.info("Follow-up check: %d actions queued to digest", len(actionable))

        await ports.with_async_retry(_core, max_retries=1, delay_seconds=60, job_name="followup_check")
        ports._mark_job_success("followup_check")
    except Exception as exc:
        ports.logger.exception("Failed to run follow-up check")
        await ports._notify_job_failure(context, chat_id, "followup_check", exc)



async def run_inactivity_check(context, ports: OperationalJobPorts) -> None:
    data = context.job.data
    chat_id = data["chat_id"]
    try:
        from config.settings import CONFIG
        from src.repositories.operator_state_repo import get_notification_mode
        from src.utils.push_guard import URGENCY_NORMAL
        from src.utils.working_hours import should_notify
        with ports.scoped_read() as conn:
            if not should_notify(CONFIG, get_notification_mode(conn), URGENCY_NORMAL):
                ports.logger.debug("Inactivity check suppressed (notification_mode=%s)", get_notification_mode(conn))
                return
        from src.portfolio.inactivity_alert import (
            check_operator_inactivity,
            format_inactivity_alert,
            is_alert_dismissed_today,
        )

        with ports.scoped_read() as conn:
            report = check_operator_inactivity(conn)
        if not report.is_inactive:
            return
        with ports.scoped_read() as conn:
            dismissed = is_alert_dismissed_today(conn)
        if dismissed:
            ports.logger.info("Inactivity alert suppressed (dismissed today)")
            return

        from telegram import InlineKeyboardButton, InlineKeyboardMarkup

        text = format_inactivity_alert(report)
        reply_markup = InlineKeyboardMarkup([
            [
                InlineKeyboardButton(text=ports.button_display("VIEW MORNING BRIEF"), callback_data="IA:VIEW_BRIEF"),
                InlineKeyboardButton(text=ports.button_display("DISMISS ALERT"), callback_data="IA:DISMISS"),
            ]
        ])
        await ports.send_persisted_message(
            context.bot,
            chat_id=chat_id,
            text=text,
            message_kind="operator_alert",
            reply_markup=reply_markup,
        )
        ports.logger.info("Inactivity alert pushed: %d business days", report.days_since_last_action)
        ports._mark_job_success("inactivity_check")
    except Exception as exc:
        ports.logger.exception("Failed to run inactivity check")
        await ports._notify_job_failure(context, chat_id, "inactivity_check", exc)



async def run_closing_suggestions(context, ports: OperationalJobPorts) -> None:
    """Suggest closing for stale projects that meet all closing criteria."""
    data = context.job.data
    chat_id = data["chat_id"]
    try:
        from src.repositories.operator_state_repo import get_notification_mode
        from src.utils.push_guard import URGENCY_NORMAL, should_push
        with ports.scoped_read() as conn:
            if not should_push(get_notification_mode(conn), URGENCY_NORMAL):
                ports.logger.debug("Closing suggestions suppressed (notification_mode=%s)", get_notification_mode(conn))
                return
        from src.portfolio.project_closing import _check_closing_candidates

        with ports.scoped_read() as conn:
            candidates = _check_closing_candidates(conn)
        for project_id in candidates:
            card = ports.build_card(
                "J",
                ports.t("sched.closing_title"),
                ports.t("sched.closing_body"),
                project_id=project_id,
            )
            await ports.send_persisted_message(
                context.bot, chat_id=chat_id,
                text=ports.format_card_text(card), project_id=project_id, message_kind="card",
                reply_markup=ports.card_to_markup(card),
            )
        if candidates:
            ports.logger.info("Closing suggestions pushed for %d project(s)", len(candidates))
        ports._mark_job_success("closing_suggestions")
    except Exception as exc:
        ports.logger.exception("Failed to run closing suggestions check")
        await ports._notify_job_failure(context, chat_id, "closing_suggestions", exc)



async def run_session_timeout(context, ports: OperationalJobPorts) -> None:
    chat_id = context.job.data["chat_id"]
    try:
        import sqlite3

        from src.telegram.session import enforce_timeouts

        with ports.scoped_write() as conn:
            timed_out = enforce_timeouts(conn)
        if timed_out:
            ports.logger.info("Timed out %d session(s): %s", len(timed_out), timed_out)
    except sqlite3.OperationalError as exc:
        if "WriteSerializer" in str(exc) or "timeout" in str(exc).lower():
            # Another job (wiki curator, proactive drafts, email mining) held
            # the write lock during LLM work — harmless; retries every 5 min.
            ports.logger.warning("Session timeout deferred — write lock busy: %s", exc)
            return
        ports.logger.exception("Failed to enforce session timeouts")
        await ports._notify_job_failure(context, chat_id, "session_timeout", exc)
    except Exception as exc:
        ports.logger.exception("Failed to enforce session timeouts")
        await ports._notify_job_failure(context, chat_id, "session_timeout", exc)



async def run_deferred_check(context, ports: OperationalJobPorts) -> None:
    data = context.job.data
    chat_id = data["chat_id"]
    try:
        from config.settings import CONFIG
        from src.repositories.operator_state_repo import get_notification_mode
        from src.utils.push_guard import URGENCY_NORMAL
        from src.utils.working_hours import should_notify
        card_suppressed = False
        with ports.scoped_read() as conn:
            if not should_notify(CONFIG, get_notification_mode(conn), URGENCY_NORMAL):
                ports.logger.debug("Deferred check suppressed (notification_mode=%s)", get_notification_mode(conn))
                card_suppressed = True
        from src.portfolio.deferred_retrigger import check_deferred_triggers, mark_triggered

        if not card_suppressed:
            with ports.scoped_read() as conn:
                triggers = check_deferred_triggers(conn)
            if not triggers:
                ports._mark_job_success("deferred_check")
            else:
                for check in triggers:
                    if not check.should_trigger:
                        continue
                    with ports.scoped_write() as conn:
                        mark_triggered(conn, check.deferred_id)
                    with ports.scoped_read() as conn:
                        target = conn.execute("SELECT project_id FROM proposed_updates WHERE update_id=?",
                                              (check.proposed_update_id,)).fetchone()
                    card = ports.build_card(
                        "I", ports.t("sched.deferred_title"),
                        ports.t("sched.deferred_body").format(reason=check.trigger_reason),
                        project_id=target[0] if target else None,
                        extra_buttons=[],  # open project evidence; no blind approval without a diff
                    )
                    await ports.send_persisted_message(
                        context.bot,
                        chat_id=chat_id,
                        text=ports.format_card_text(card),
                        message_kind="card",
                        reply_markup=ports.card_to_markup(card),
                        object_links=[{"object_type": "proposed_update", "object_ref": check.proposed_update_id}],
                    )
                ports.logger.info("Deferred check: %d item(s) triggered", len(triggers))
            ports._mark_job_success("deferred_check")
    except Exception as exc:
        ports.logger.exception("Failed to run deferred check")
        await ports._notify_job_failure(context, chat_id, "deferred_check", exc)

    # Re-notify emails suppressed outside operator hours — queue to digest
    try:
        from src.email.inbound_policy import check_deferred_email_notifications

        from src.repositories.digest_queue_repo import enqueue_digest_item

        with ports.scoped_write() as conn:
            email_notifs = check_deferred_email_notifications(conn)
            for notif in email_notifs:
                enqueue_digest_item(
                    conn,
                    category="email_deferred",
                    title=notif["title"],
                    body=notif["body"],
                    project_id=notif.get("project_id"),
                    commit=False,
                )
        if email_notifs:
            ports.logger.info("Deferred email re-notification: %d item(s) queued to digest", len(email_notifs))
    except Exception as exc:
        ports.logger.exception("Failed to run deferred email re-notification")
        await ports._notify_job_failure(context, chat_id, "deferred_email_renotify", exc)

