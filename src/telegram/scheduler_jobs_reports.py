"""Weekly and monthly operational reports; scheduling remains in the facade."""
from __future__ import annotations

from src.telegram.scheduler_jobs_operational import OperationalJobPorts


async def run_weekly_report(context, ports: OperationalJobPorts) -> None:
    data = context.job.data
    chat_id = data["chat_id"]
    try:
        from src.repositories.operator_state_repo import get_notification_mode
        from src.utils.push_guard import URGENCY_NORMAL, should_push
        with ports.scoped_read() as conn:
            if not should_push(get_notification_mode(conn), URGENCY_NORMAL):
                ports.logger.debug("Weekly report suppressed (notification_mode=%s)", get_notification_mode(conn))
                return
        from datetime import datetime, timezone, timedelta
        from src.portfolio.periodic_reports import format_report_markdown, generate_weekly_report

        now = datetime.now(timezone.utc)
        with ports.scoped_read() as conn:
            report = generate_weekly_report(conn, (now - timedelta(days=7)).strftime("%Y-%m-%d"), now.strftime("%Y-%m-%d"))
        text = format_report_markdown(report)
        if len(text) > 4000:
            text = text[:3950] + "\n\n... (truncated)"
        await ports.send_persisted_message(context.bot, chat_id=chat_id, text=text, message_kind="report")
        ports.logger.info("Weekly report sent: %d active projects", report.total_active)
        ports._mark_job_success("weekly_report")
    except Exception as exc:
        ports.logger.exception("Failed to send weekly report")
        await ports._notify_job_failure(context, chat_id, "weekly_report", exc)



async def run_monthly_report(context, ports: OperationalJobPorts) -> None:
    data = context.job.data
    chat_id = data["chat_id"]
    try:
        from src.repositories.operator_state_repo import get_notification_mode
        from src.utils.push_guard import URGENCY_NORMAL, should_push
        with ports.scoped_read() as conn:
            if not should_push(get_notification_mode(conn), URGENCY_NORMAL):
                ports.logger.debug("Monthly report suppressed (notification_mode=%s)", get_notification_mode(conn))
                return
        from datetime import datetime, timezone
        from src.portfolio.periodic_reports import format_report_markdown, generate_monthly_report

        now = datetime.now(timezone.utc)
        with ports.scoped_read() as conn:
            report = generate_monthly_report(conn, now.replace(day=1).strftime("%Y-%m-%d"), now.strftime("%Y-%m-%d"))
        text = format_report_markdown(report)
        if len(text) > 4000:
            text = text[:3950] + "\n\n... (truncated)"
        await ports.send_persisted_message(context.bot, chat_id=chat_id, text=text, message_kind="report")
        ports.logger.info("Monthly report sent: %d active projects", report.total_active)
        ports._mark_job_success("monthly_report")
    except Exception as exc:
        ports.logger.exception("Failed to send monthly report")
        await ports._notify_job_failure(context, chat_id, "monthly_report", exc)

