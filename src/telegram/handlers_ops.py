"""Handlers for SLA Card (F), Follow-up Card (G), Brief Card (I), Closing Checklist Card (J)."""

from __future__ import annotations

import sqlite3
import re
from datetime import datetime, timezone, timedelta

from src.events.bus import Event, event_bus
from src.repositories.activity_log_repo import insert_activity
from src.repositories.contact_repo import find_internal_contacts_by_expertise
from src.telegram.i18n import t
from src.portfolio.followup_tracker import (
    cancel_followup,
    complete_followup,
    get_followup,
    reassign_followup,
    snooze_followup,
)
from src.telegram.cards import build_card, format_card_text
from src.telegram.markup import card_to_markup
from src.telegram.session import create_session


def handle_sla(
    conn: sqlite3.Connection,
    action: str,
    project_id: str | None,
    *,
    message_context: dict | None = None,
    operator_id: str = "default_operator",
    owned_draft_scope: bool = False,
) -> dict:
    """Handle SLA Alert Card (M) actions."""
    if action in ("SNOOZE_1DAY", "SNOOZE") and project_id:
        from datetime import timedelta
        from src.repositories.working_memory_repo import upsert_memory

        snoozed_until = (
            datetime.now(timezone.utc) + timedelta(hours=24)
        ).isoformat()
        upsert_memory(conn, project_id, "sla_snoozed_until", snoozed_until)
        insert_activity(
            conn,
            actor="operator",
            action_type="sla_snoozed",
            project_id=project_id,
            surface="telegram",
            summary=f"SLA alert snoozed until {snoozed_until[:10]}",
        )
        conn.commit()
        event_bus.publish(Event(
            event_type="sla_snoozed",
            project_id=project_id,
            surface="telegram",
        ))
        return {"text": t("sla.snoozed_24h").format(project_id=project_id), "project_id": project_id}

    if action == "MARK_DONE":
        # Try to complete related followup from message context
        followup_id = None
        if message_context:
            for link in message_context.get("links", []):
                if link.get("object_type") == "followup":
                    followup_id = link.get("object_ref")
                    break
        if followup_id:
            from src.portfolio.followup_tracker import complete_followup
            if not complete_followup(conn, followup_id):
                return {"text": t("followup.could_not_complete"), "project_id": project_id}
            insert_activity(
                conn,
                actor="operator",
                action_type="sla_marked_done",
                project_id=project_id,
                surface="telegram",
                object_ref=followup_id,
                summary=f"SLA follow-up {followup_id} marked done",
            )
        else:
            insert_activity(
                conn,
                actor="operator",
                action_type="sla_marked_done",
                project_id=project_id,
                surface="telegram",
                summary=f"SLA alert marked done for {project_id}",
            )
        conn.commit()
        return {"text": (t("followup.completed").format(followup_id=followup_id) if followup_id else t("sla.acknowledged").format(project_id=project_id)), "project_id": project_id}

    if action in ("ESCALATE", "ACK") and project_id:
        insert_activity(
            conn,
            actor="operator",
            action_type="sla_escalated" if action == "ESCALATE" else "sla_acknowledged",
            project_id=project_id,
            surface="telegram",
            summary=f"SLA {'escalated' if action == 'ESCALATE' else 'acknowledged'} for {project_id}",
        )
        conn.commit()
        if action == "ESCALATE":
            event_bus.publish(Event(
                event_type="sla_escalated",
                project_id=project_id,
                surface="telegram",
            ))
        text = t("sla.escalated").format(project_id=project_id) if action == "ESCALATE" else t("sla.acknowledged").format(project_id=project_id)
        return {"text": text, "project_id": project_id}

    if action in ("DRAFT_STATUS_EMAIL", "DRAFT STATUS EMAIL"):
        from src.portfolio.draft_engine import generate_draft_for_signal

        row = conn.execute(
            "SELECT target_go_live, current_milestone, "
            "CAST(julianday(target_go_live) - julianday('now') AS INTEGER) AS days_until "
            "FROM projects WHERE project_id = ?",
            (project_id,),
        ).fetchone()

        context_hints: dict = {"days_until_golive": 0, "current_milestone": ""}
        if row:
            context_hints = {
                "days_until_golive": row["days_until"] or 0,
                "current_milestone": row["current_milestone"] or "",
            }

        chat_id = (message_context or {}).get("chat_id", 0)
        draft_id = generate_draft_for_signal(
            None if owned_draft_scope else conn,
            signal_type="sla_deadline",
            project_id=project_id,
            context_hints=context_hints,
            chat_id=chat_id,
        )
        if draft_id:
            return {"action": "draft_status_email", "draft_id": draft_id, "message": t("sla.draft_status_generated"), "text": t("sla.draft_status_generated"), "project_id": project_id}
        return {"action": "draft_status_email", "draft_id": None, "message": t("sla.draft_skipped_pending"), "text": t("sla.draft_skipped_pending"), "project_id": project_id}

    return {"text": t("sla.generic_action").format(action=action), "project_id": project_id}



def _parse_revision_bound_followup_ref(value: str | None) -> tuple[str | None, int | None, str | None]:
    """Parse ``followup_id@revision`` with an optional ``_contact_id`` suffix."""
    if not value:
        return None, None, None
    match = re.fullmatch(r"(?P<followup_id>.+)@(?P<revision>\d+)(?:_(?P<contact_id>.+))?", value)
    if not match:
        return value, None, None
    return (
        match.group("followup_id"),
        int(match.group("revision")),
        match.group("contact_id"),
    )


def _stale_followup_card(followup_id: str | None) -> dict:
    return {
        "text": t("followup.stale_card"),
        "object_ref": followup_id,
        "refresh_required": True,
    }

def handle_followup(
    conn: sqlite3.Connection,
    action: str,
    project_id: str | None,
    *,
    message_context: dict | None = None,
    operator_id: str = "default_operator",
    owned_draft_scope: bool = False,
) -> dict:
    """Handle Follow-up Card (G) actions."""
    followup_id, expected_revision, bound_contact_id = _parse_revision_bound_followup_ref(project_id)

    revision_required_actions = {"DONE", "SNOOZE", "CANCEL", "CHANGE_OWNER", "REASSIGN", "PICK_OWNER"}
    if action in revision_required_actions and expected_revision is None:
        return _stale_followup_card(followup_id)

    if action == "DONE" and followup_id:
        ok = complete_followup(conn, followup_id, expected_revision=expected_revision)
        if ok:
            event_bus.publish(Event(
                event_type="followup_completed",
                detail={"followup_id": followup_id},
                surface="telegram",
            ))
        return {"text": t("followup.completed").format(followup_id=followup_id) if ok else t("followup.could_not_complete"), "object_ref": followup_id}

    if action == "SNOOZE" and followup_id:
        new_due = (datetime.now(timezone.utc).astimezone(timezone(timedelta(hours=8))) + timedelta(days=1)).strftime("%Y-%m-%d")
        ok, reason = snooze_followup(conn, followup_id, new_due, expected_revision=expected_revision)
        if ok:
            return {"text": t("followup.snoozed").format(followup_id=followup_id, new_due=new_due), "object_ref": followup_id}
        if reason == "max_snoozes":
            return {"text": t("followup.max_snoozes").format(followup_id=followup_id), "object_ref": followup_id}
        return {"text": t("followup.could_not_snooze").format(followup_id=followup_id), "object_ref": followup_id}

    if action == "PICK_OWNER" and followup_id:
        # Format: G:PICK_OWNER:{followup_id}@{revision}_{contact_id}
        fup_id, contact_id = followup_id, bound_contact_id
        if not contact_id or not conn.execute(
            "SELECT 1 FROM contacts WHERE contact_id=?", (contact_id,),
        ).fetchone():
            return {"text": t("followup.invalid_owner"), "object_ref": followup_id}
        if not reassign_followup(
            conn, fup_id, contact_id, expected_revision=expected_revision, commit=False,
        ):
            return {"text": t("followup.invalid_owner"), "object_ref": fup_id}
        insert_activity(
            conn,
            actor="operator",
            action_type="followup_reassigned",
            project_id=None,
            surface="telegram",
            object_ref=fup_id,
            summary=f"Follow-up reassigned to contact {contact_id}",
        )
        conn.commit()
        return {"text": t("followup.reassigned").format(followup_id=fup_id), "object_ref": fup_id}

    if action in {"REASSIGN", "CHANGE_OWNER"}:
        fup = get_followup(conn, followup_id) if followup_id else None
        expertise_query = (fup["description"] or "follow-up") if fup else "follow-up"
        candidates = find_internal_contacts_by_expertise(conn, expertise_query, limit=3)

        if candidates:
            extra_buttons = []
            for c in candidates:
                score = c.get("responsiveness_score") or 0.0
                label = f"{c['display_name']} ({score:.2f})"
                callback_data = f"G:PICK_OWNER:{followup_id}@{expected_revision}_{c['contact_id']}"
                extra_buttons.append([(label, callback_data)])
            # "Other..." triggers free-text session via REASSIGN action
            extra_buttons.append([("Other...", f"G:REASSIGN:{followup_id}@{expected_revision}")])

            card = build_card(
                "G",
                "Select New Owner",
                t("followup.select_owner_body").format(expertise_query=expertise_query),
                project_id=followup_id,
                extra_buttons=extra_buttons,
                action_revision=expected_revision,
            )
            card.metadata = {"project_id": fup.get("project_id") if fup else None}
            return {
                "text": format_card_text(card),
                "reply_markup": card_to_markup(card),
                "object_ref": followup_id,
            }

        # No candidates — fall through to free-text session
        session_id = create_session(
            conn,
            "followup_reassign",
            originating_ref=followup_id,
            operator_id=operator_id,
        )
        return {
            "text": t("followup.send_assignee_name"),
            "session_id": session_id,
            "object_ref": followup_id,
            "object_links": [
                {"object_type": "telegram_session", "object_ref": session_id},
            ],
        }

    if action == "CANCEL" and followup_id:
        ok = cancel_followup(conn, followup_id, expected_revision=expected_revision)
        if ok:
            event_bus.publish(Event(
                event_type="followup_cancelled",
                detail={"followup_id": followup_id},
                surface="telegram",
            ))
        return {"text": t("followup.cancelled").format(followup_id=followup_id) if ok else t("followup.could_not_cancel"), "object_ref": followup_id}

    if action in ("DRAFT_EMAIL", "DRAFT EMAIL"):
        from src.portfolio.draft_engine import generate_draft_for_signal

        row = conn.execute(
            "SELECT followup_id, description, "
            "CAST(julianday('now') - julianday(due_date) AS INTEGER) AS days_overdue "
            "FROM internal_followups "
            "WHERE project_id = ? AND status = 'open' AND due_date < date('now') "
            "ORDER BY due_date ASC LIMIT 1",
            (project_id,),
        ).fetchone()

        context_hints: dict = {"followup_description": "", "days_overdue": 0}
        if row:
            context_hints = {
                "followup_description": row["description"] or "",
                "days_overdue": row["days_overdue"] or 0,
            }

        chat_id = (message_context or {}).get("chat_id", 0)
        draft_id = generate_draft_for_signal(
            None if owned_draft_scope else conn,
            signal_type="overdue_followup",
            project_id=project_id,
            context_hints=context_hints,
            chat_id=chat_id,
        )
        if draft_id:
            return {"action": "draft_email", "draft_id": draft_id, "message": t("followup.draft_generated"), "text": t("followup.draft_generated"), "project_id": project_id}
        return {"action": "draft_email", "draft_id": None, "message": t("followup.draft_skipped_pending"), "text": t("followup.draft_skipped_pending"), "project_id": project_id}

    return {"text": t("followup.generic_action").format(action=action), "object_ref": followup_id}


def handle_brief(
    conn: sqlite3.Connection,
    action: str,
    project_id: str | None,
    *,
    message_context: dict | None = None,
    operator_id: str = "default_operator",
) -> dict:
    """Handle Morning Brief Card (I) actions: RE-EVALUATE | DISMISS | SNOOZE 7DAYS."""
    if action == "RE-EVALUATE":
        from src.portfolio.morning_brief import generate_morning_brief, format_brief_with_llm
        from src.telegram.cards import build_card, format_card_text
        from src.telegram.markup import card_to_markup
        brief = generate_morning_brief(conn)
        card = build_card("I", "Morning Brief", format_brief_with_llm(brief))
        insert_activity(
            conn,
            actor="operator",
            action_type="brief_re_evaluated",
            surface="telegram",
            summary="Operator re-evaluated morning brief",
        )
        conn.commit()
        return {
            "text": format_card_text(card),
            "message_kind": "card",
            "reply_markup": card_to_markup(card),
        }

    if action == "DISMISS":
        insert_activity(
            conn,
            actor="operator",
            action_type="brief_dismissed",
            surface="telegram",
            summary="Operator dismissed morning brief",
        )
        conn.commit()
        return {"text": t("brief.dismissed")}

    if action in ("SNOOZE 7DAYS", "SNOOZE_7DAYS"):
        from src.repositories.working_memory_repo import upsert_memory
        snoozed_until = (datetime.now(timezone.utc) + timedelta(days=7)).isoformat()
        upsert_memory(conn, "_system", "brief_snoozed_until", snoozed_until)
        insert_activity(
            conn,
            actor="operator",
            action_type="brief_snoozed",
            surface="telegram",
            summary=f"Morning brief snoozed for 7 days (until {snoozed_until[:10]})",
        )
        conn.commit()
        return {"text": t("brief.snoozed_7days").format(date=snoozed_until[:10])}

    if action == "ACK":
        return {"text": t("brief.acknowledged")}
    return {"text": t("brief.generic_action").format(action=action)}


def handle_inactivity(
    conn: sqlite3.Connection,
    action: str,
    project_id: str | None,
    *,
    message_context: dict | None = None,
    operator_id: str = "default_operator",
) -> dict:
    """Handle Inactivity Alert Card (IA) and Closing Checklist Card (J).

    Profile IA (Section 34): VIEW_BRIEF | DISMISS
    Profile J (Section 30): COMPLETE CLOSING | POSTPONE | ACK (inactivity acknowledgement)
    """
    # ── IA: Inactivity Alert actions (Section 34) ─────────────────────────────
    if action == "VIEW_BRIEF":
        # Show morning brief as text (no streaming needed for a daily summary)
        from src.portfolio.morning_brief import generate_morning_brief, format_brief_text
        brief = generate_morning_brief(conn)
        text = format_brief_text(brief)
        return {"text": text, "message_kind": "command_reply"}

    if action == "DISMISS":
        # Per Section 34.3: acknowledges alert for today only
        from datetime import timedelta
        from src.repositories.operator_state_repo import dismiss_inactivity_alert

        dismissed_until = (
            datetime.now(timezone.utc) + timedelta(hours=20)
        ).isoformat()
        dismiss_inactivity_alert(conn, dismissed_until)
        insert_activity(
            conn,
            actor="operator",
            action_type="inactivity_alert_dismissed",
            surface="telegram",
            summary="Inactivity alert dismissed for today",
        )
        conn.commit()
        return {"text": t("inactivity.dismissed")}

    # ── Profile J: ACK (inactivity acknowledgement — for J profile backwards compat) ──
    if action == "ACK":
        insert_activity(
            conn,
            actor="operator",
            action_type="inactivity_acknowledged",
            surface="telegram",
            summary="Operator acknowledged inactivity alert",
        )
        conn.commit()
        return {"text": t("inactivity.acknowledged")}

    # ── Profile J: Closing Checklist actions (Section 30) ─────────────────────
    if action in (
        "OPEN CLOSING REVIEW",
        "OPEN_CLOSING_REVIEW",
        "COMPLETE CLOSING",
        "COMPLETE_CLOSING",
    ):
        if not project_id:
            return {"text": t("closing.no_project")}
        from src.telegram.closing_advisory import build_closing_advisory

        return {
            "text": build_closing_advisory(conn, project_id),
            "project_id": project_id,
        }

    if action == "POSTPONE":
        if not project_id:
            return {"text": t("closing.no_project")}
        from datetime import timedelta
        from src.repositories.working_memory_repo import upsert_memory

        postpone_days = 7  # default per Blueprint Section 30.5
        postpone_until = (
            datetime.now(timezone.utc) + timedelta(days=postpone_days)
        ).isoformat()
        upsert_memory(conn, project_id, "closing_postponed_until", postpone_until)
        insert_activity(
            conn,
            actor="operator",
            action_type="closing_postponed",
            surface="telegram",
            project_id=project_id,
            summary=f"Closing checklist postponed for {postpone_days} days (until {postpone_until})",
        )
        conn.commit()
        return {
            "text": t("closing.postponed").format(days=postpone_days, date=postpone_until[:10]),
            "project_id": project_id,
        }

    return {"text": t("closing.generic_action").format(action=action)}


def handle_error(
    conn: sqlite3.Connection,
    action: str,
    project_id: str | None,
    *,
    message_context: dict | None = None,
    operator_id: str = "default_operator",
) -> dict:
    """Handle LLM Error Card (N) actions: RETRY or DISMISS."""
    if action == "RETRY":
        import json
        from src.telegram.error_notices import error_notice
        try:
            raw = (message_context or {}).get("metadata_json") or "{}"
            meta = json.loads(raw) if isinstance(raw, str) else raw
        except (ValueError, TypeError):
            meta = {}
        task = meta.get("task", "unknown")
        recovery = None
        if meta.get("trace_id"):
            from src.repositories.working_memory_repo import get_memory
            stored = get_memory(conn, project_id or "_system", f"error_retry:{meta['trace_id']}")
            if stored and stored.get("memory_value"):
                try:
                    recovery = json.loads(stored["memory_value"])
                    if isinstance(recovery, dict):
                        task = recovery.get("task_type") or task
                    else:
                        recovery = None
                except (ValueError, TypeError):
                    recovery = None
        insert_activity(conn, actor="operator", action_type="llm_error_recovery_viewed",
            project_id=project_id, surface="telegram", summary=f"Viewed recovery information for {task}")
        conn.commit()
        response = {"text": error_notice(task, meta.get("error_class", ""), reference=meta.get("trace_id")),
                    "project_id": project_id, "retry_started": False}
        if recovery is not None:
            response["retry_context"] = recovery  # compatibility data; never an execution instruction
            if recovery.get("input_summary"):
                from src.telegram.operator_messages import en
                response["text"] += "\n" + ("Original request: " if en() else "原要求：") + str(recovery["input_summary"])[:350]
        return response

    if action == "DISMISS":
        insert_activity(
            conn,
            actor="operator",
            action_type="llm_error_dismissed",
            project_id=project_id,
            surface="telegram",
            summary="LLM error alert dismissed",
        )
        conn.commit()
        return {"text": t("error.llm_dismissed"), "project_id": project_id}

    return {"text": t("error.generic_action").format(action=action), "project_id": project_id}


# handle_attach moved to src/telegram/handlers_ticket.py
from src.telegram.handlers_ticket import handle_attach  # noqa: F401  re-export
