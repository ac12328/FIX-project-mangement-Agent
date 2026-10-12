"""Operational follow-up and draft lifecycle scheduler jobs."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable


@dataclass(frozen=True)
class ActionJobPorts:
    """Runtime dependencies supplied by the scheduler facade."""
    scoped_read: Callable[..., Any]
    scoped_write: Callable[..., Any]
    send_persisted_message: Callable[..., Any]
    _mark_job_success: Callable[..., Any]
    _notify_job_failure: Callable[..., Any]
    with_async_retry: Callable[..., Any]
    t: Callable[..., Any]
    logger: Any


async def run_resolution_followup(context, ports: ActionJobPorts) -> None:
    """Push 'How did you handle this?' messages for timed-out resolution_learning sessions."""
    data = context.job.data
    chat_id = data["chat_id"]
    try:
        from config.settings import CONFIG
        from src.repositories.operator_state_repo import get_notification_mode
        from src.utils.push_guard import URGENCY_NORMAL
        from src.utils.working_hours import should_notify
        with ports.scoped_read() as conn:
            if not should_notify(CONFIG, get_notification_mode(conn), URGENCY_NORMAL):
                ports.logger.debug("Resolution followup suppressed (notification_mode=%s)", get_notification_mode(conn))
                return
        async def _core():
            from src.advisory.jobs import check_resolution_followups

            with ports.scoped_write() as conn:
                followups = check_resolution_followups(conn)
            for fu in followups:
                await ports.send_persisted_message(
                    context.bot,
                    chat_id=chat_id,
                    text=fu["message"],
                    project_id=fu.get("project_id"),
                    message_kind="system",
                )
            if followups:
                ports.logger.info("Resolution followup: pushed %d messages", len(followups))

        await ports.with_async_retry(_core, max_retries=1, delay_seconds=60, job_name="resolution_followup")
        ports._mark_job_success("resolution_followup")
    except Exception as exc:
        ports.logger.exception("Failed to run resolution followup check")
        await ports._notify_job_failure(context, chat_id, "resolution_followup", exc)


async def run_support_ticket_aging(context, ports: ActionJobPorts) -> None:
    """Push reminder card for open support tickets open > 4 hours with no recent reminder."""
    data = context.job.data
    chat_id = data["chat_id"]
    try:
        from config.settings import CONFIG
        from src.repositories.operator_state_repo import get_notification_mode
        from src.utils.push_guard import URGENCY_URGENT
        from src.utils.working_hours import should_notify
        with ports.scoped_read() as conn:
            if not should_notify(CONFIG, get_notification_mode(conn), URGENCY_URGENT):
                ports.logger.debug("Support ticket aging suppressed (notification_mode=%s)", get_notification_mode(conn))
                return

        async def _core():
            from datetime import datetime, timezone, timedelta
            from src.repositories.ticket_repo import get_tickets_by_status
            from src.repositories.activity_log_repo import insert_activity

            now = datetime.now(timezone.utc)
            cutoff = now - timedelta(hours=4)
            cutoff_str = cutoff.strftime('%Y-%m-%d %H:%M:%S')  # SQLite-compatible format

            with ports.scoped_read() as conn:
                open_tickets = get_tickets_by_status(conn, "open", limit=100)
            for ticket in open_tickets:
                if not ticket.reported_at:
                    continue
                reported = ticket.reported_at
                if reported.tzinfo is None:
                    reported = reported.replace(tzinfo=timezone.utc)
                if reported >= cutoff:
                    continue

                with ports.scoped_read() as conn:
                    recent = conn.execute(
                        """SELECT 1 FROM activity_log
                           WHERE object_ref = ? AND action_type = 'support_aging_reminder'
                           AND created_at >= ? LIMIT 1""",
                        (ticket.ticket_id, cutoff_str),
                    ).fetchone()
                if recent:
                    continue

                elapsed = int((now - reported).total_seconds() / 3600)
                from src.repositories.digest_queue_repo import enqueue_digest_item
                with ports.scoped_write() as conn:
                    enqueue_digest_item(
                        conn,
                        category="ticket_aging",
                        title=f"🎫 {ticket.ticket_id}",
                        body=f"{ticket.title[:60]} — Open {elapsed}h, Counterparty: {ticket.counterparty or 'Unknown'}",
                        project_id=None,
                        metadata={"ticket_id": ticket.ticket_id, "elapsed_hours": elapsed},
                        commit=False,
                    )
                    insert_activity(
                        conn, actor="system", action_type="support_aging_reminder",
                        surface="scheduler", object_ref=ticket.ticket_id,
                        summary=f"Aging reminder queued for digest: {ticket.ticket_id}",
                    )

        await ports.with_async_retry(_core, max_retries=1, delay_seconds=60, job_name="support_ticket_aging")
        ports._mark_job_success("support_ticket_aging")
    except Exception as exc:
        ports.logger.exception("Support ticket aging job failed")
        await ports._notify_job_failure(context, chat_id, "support_ticket_aging", exc)


async def run_confidence_lifecycle(context, ports: ActionJobPorts) -> None:
    """Select governed v2 or preserve the exact legacy confidence lifecycle."""
    from config import defaults
    data = context.job.data
    chat_id = data["chat_id"]
    try:
        results = {}

        async def _core():
            if defaults.MEMORY_LIFECYCLE_V2_ENABLED and not defaults.MEMORY_LIFECYCLE_KILL_SWITCH:
                from src.learning.lifecycle_service import run_lifecycle_maintenance
                summary = run_lifecycle_maintenance()
                ports.logger.info("Governed lifecycle: expired=%d review_required=%d revalidated=%d",
                            summary.expired, summary.review_required, summary.revalidated)
                return
            from src.learning.confidence_lifecycle import (
                auto_approve_pending_rules, decay_stale_rules, promote_proven_rules,
                advance_wiki_page_statuses,
            )
            from src.wiki.graph import invalidate_wiki_graph
            from src.wiki.indexer import invalidate_wiki_index
            with ports.scoped_write() as conn:
                approved = auto_approve_pending_rules(conn)
                if approved:
                    ports.logger.info("Confidence lifecycle: auto-approved %d rules", approved)
                decayed = decay_stale_rules(conn)
                promoted = promote_proven_rules(conn)
            wiki_advanced = advance_wiki_page_statuses()
            if wiki_advanced:
                invalidate_wiki_graph()
                invalidate_wiki_index()
            if decayed or promoted or wiki_advanced:
                ports.logger.info("Confidence lifecycle: %d decayed, %d promoted, %d wiki pages advanced",
                            decayed, promoted, wiki_advanced)
            results.update(approved=approved, decayed=decayed, promoted=promoted, wiki_advanced=wiki_advanced)

        await ports.with_async_retry(_core, max_retries=1, delay_seconds=60, job_name="confidence_lifecycle")
        ports._mark_job_success("confidence_lifecycle")
        if results and sum(results.values()) > 0:
            summary = (
                "📊 Confidence Lifecycle Summary\n"
                "━━━━━━━━━━━━━━━━━━━━━━━\n"
                f"• {results['approved']} rules auto-approved\n"
                f"• {results['decayed']} rules decayed\n"
                f"• {results['promoted']} rules promoted to KB\n"
                f"• {results['wiki_advanced']} wiki pages advanced\n\n"
                "No operator action needed."
            )
            await ports.send_persisted_message(context.bot, chat_id=chat_id, text=summary, message_kind="notification")
    except Exception as exc:
        ports.logger.exception("Confidence lifecycle job failed")
        await ports._notify_job_failure(context, chat_id, "confidence_lifecycle", exc)


async def run_proactive_drafts(context, ports: ActionJobPorts) -> None:
    """Daily (weekdays 11:30 HKT): generate follow-up drafts for stale External projects."""
    data = context.job.data
    chat_id = data["chat_id"]

    try:
        from config.settings import CONFIG
        from src.repositories.operator_state_repo import get_notification_mode
        from src.utils.push_guard import URGENCY_NORMAL, should_push
        from src.utils.working_hours import is_within_working_hours

        # Guards must run inside the try so a failure is reported via
        # _notify_job_failure instead of crashing the job silently.
        # is_within_working_hours requires the AppConfig argument.
        if not is_within_working_hours(CONFIG):
            return
        with ports.scoped_read() as conn:
            mode = get_notification_mode(conn)
        if not should_push(mode, URGENCY_NORMAL):
            ports.logger.debug("proactive_drafts suppressed (notification_mode=%s)", mode)
            return

        from src.portfolio.proactive_drafter import generate_proactive_drafts

        # conn=None so draft_engine does not hold the write lock during LLM calls.
        results = generate_proactive_drafts(None, max_drafts=3, chat_id=chat_id)

        if results:
            generated = [r for r in results if r.get("draft_id")]
            skipped = len(results) - len(generated)
            ports.logger.info(
                "proactive_drafts: %d generated, %d skipped (dedup/cooldown)",
                len(generated), skipped,
            )
        ports._mark_job_success("proactive_drafts")
    except Exception as exc:
        ports.logger.exception("Proactive drafts job failed")
        await ports._notify_job_failure(context, chat_id, "proactive_drafts", exc)


async def run_draft_expiry(context, ports: ActionJobPorts) -> None:
    """Nightly 02:00 HKT: auto-discard drafts in 'created' status older than 14 days."""
    from src.repositories.operator_state_repo import get_notification_mode
    from src.utils.push_guard import URGENCY_NORMAL, should_push

    data = context.job.data
    chat_id = data["chat_id"]

    try:
        from config.settings import CONFIG
        expiry_days = CONFIG.thresholds.draft_expiry_days

        with ports.scoped_write() as conn:
            rows = conn.execute(
                "SELECT draft_id, project_id, subject "
                "FROM drafts "
                "WHERE status = 'created' "
                f"  AND created_at < datetime('now', '-{expiry_days} days') "
                "ORDER BY created_at ASC",
            ).fetchall()

            if not rows:
                ports._mark_job_success("draft_expiry")
                return

            ids = [r["draft_id"] for r in rows]
            placeholders = ",".join("?" * len(ids))
            conn.execute(
                f"UPDATE drafts SET status = 'discarded' "
                f"WHERE draft_id IN ({placeholders})",
                ids,
            )
            conn.commit()

        with ports.scoped_read() as rconn:
            mode = get_notification_mode(rconn)
        if chat_id and should_push(mode, URGENCY_NORMAL):
            lines = [ports.t("sched.draft_expiry").format(count=len(rows))]
            for r in rows[:10]:
                proj = r["project_id"] or "\u2014"
                subj = (r["subject"] or "No subject")[:50]
                lines.append(f"\u2022 [{proj}] {subj}")
            if len(rows) > 10:
                lines.append(ports.t("sched.draft_expiry_more").format(count=len(rows) - 10))
            await ports.send_persisted_message(
                context.bot, chat_id=chat_id,
                text="\n".join(lines),
                message_kind="system",
            )

        ports._mark_job_success("draft_expiry")
    except Exception as exc:
        ports.logger.exception("Draft expiry job failed")
        await ports._notify_job_failure(context, chat_id, "draft_expiry", exc)


async def run_escalation_check(context, ports: ActionJobPorts) -> None:
    """Daily (weekdays 14:00 HKT): auto-escalate RED-aged External projects.

    Scans Active+External projects, classifies aging via AgingDetectionSkill,
    and pushes escalation alerts for RED projects not already alerted today.
    """
    data = context.job.data
    chat_id = data["chat_id"]

    try:
        from config.settings import CONFIG
        from src.repositories.operator_state_repo import get_notification_mode
        from src.utils.push_guard import URGENCY_URGENT, should_push
        from src.utils.working_hours import is_within_working_hours
        from src.skills.aging_detection import EXTERNAL_AGING_RED_DAYS

        # Guards inside the try so failures are reported via _notify_job_failure.
        # is_within_working_hours requires the AppConfig argument; read the real
        # operator notification_mode instead of hardcoding "active".
        if not is_within_working_hours(CONFIG):
            return
        with ports.scoped_read() as conn:
            mode = get_notification_mode(conn)
        if not should_push(mode, URGENCY_URGENT):
            ports.logger.debug("escalation_check suppressed (notification_mode=%s)", mode)
            return

        from datetime import datetime as _dt, timezone as _tz
        from src.repositories.working_memory_repo import get_memory, upsert_memory

        escalated: list[str] = []

        with ports.scoped_read() as conn:
            rows = conn.execute(
                """SELECT project_id, client_name, current_milestone, ball_with, last_activity_at
                   FROM projects
                   WHERE project_status = 'Active' AND ball_with = 'External'"""
            ).fetchall()

        today_str = _dt.now(_tz.utc).strftime("%Y-%m-%d")

        with ports.scoped_write() as conn:
            for row in rows:
                pid = row["project_id"]
                last_at = row["last_activity_at"]
                if not last_at:
                    continue
                try:
                    last_dt = _dt.fromisoformat(last_at)
                    if last_dt.tzinfo is None:
                        last_dt = last_dt.replace(tzinfo=_tz.utc)
                    days_inactive = (_dt.now(_tz.utc) - last_dt).days
                except (ValueError, TypeError):
                    continue

                if days_inactive < EXTERNAL_AGING_RED_DAYS:
                    continue

                # Dedup: check if already escalated today
                mem_key = f"escalation_alert_{pid}"
                entry = get_memory(conn, "_system", mem_key)
                if entry and entry.get("memory_value") == today_str:
                    continue

                escalated.append(
                    ports.t("sched.escalation_item").format(
                        client_name=row['client_name'] or pid,
                        days=days_inactive,
                        milestone=row['current_milestone'] or '?',
                    )
                )
                upsert_memory(conn, "_system", mem_key, today_str)

        if escalated and chat_id:
            text = (
                ports.t("sched.escalation_alert").format(count=len(escalated)) + "\n\n"
                + "\n".join(escalated)
                + ports.t("sched.escalation_consider")
            )
            await ports.send_persisted_message(
                context.bot, chat_id=chat_id, text=text, message_kind="system",
            )
        ports._mark_job_success("escalation_check")
    except Exception as exc:
        ports.logger.exception("Escalation check job failed")
        await ports._notify_job_failure(context, chat_id, "escalation_check", exc)
