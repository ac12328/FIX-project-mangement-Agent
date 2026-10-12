"""
Operator Inactivity Alert (Blueprint Section 34).
Tracks Telegram-specific operator interaction; alerts after 3 business days of inactivity.
Alert repeats every business day until operator interaction is detected.
Does NOT suppress, cancel, or auto-resolve any pending items.
"""

import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timezone, timedelta

from src.utils.calendar import (
    INACTIVITY_THRESHOLD_DAYS,
    is_business_day as _is_business_day,
    business_days_between as _business_days_between,
    next_business_day as _next_business_day,
)
from src.telegram.i18n import t


@dataclass
class InactivityReport:
    """Result of the inactivity check (Blueprint Section 34)."""
    is_inactive: bool
    days_since_last_action: int | None  # business days
    last_action_at: str | None
    unacknowledged_cards: int = 0  # needs_review ProposedUpdates
    escalation_l2_projects: int = 0  # projects with escalation_level=2
    overdue_followups: int = 0
    dismissed_until: str | None = None
    alert_items: list[str] = field(default_factory=list)


def check_operator_inactivity(conn: sqlite3.Connection) -> InactivityReport:
    """
    Check if the operator has been inactive for INACTIVITY_THRESHOLD_DAYS business days.
    Tracks Telegram-specific interactions only (per Section 34.4).
    Returns InactivityReport with pending item summary.
    """
    from src.repositories.operator_state_repo import get_operator_state

    state = get_operator_state(conn)
    last_action_at = state.get("last_telegram_interaction_at") if state else None
    dismissed_until = state.get("inactivity_alert_dismissed_until") if state else None

    now = datetime.now(timezone.utc)
    days_since = None

    if last_action_at:
        try:
            last_dt = datetime.fromisoformat(last_action_at)
            if last_dt.tzinfo is None:
                last_dt = last_dt.replace(tzinfo=timezone.utc)
            # Business days between last interaction and now
            days_since = _business_days_between(last_dt, now)
        except (ValueError, TypeError):
            pass

    is_inactive = (
        days_since is not None
        and days_since >= INACTIVITY_THRESHOLD_DAYS
    )

    # Count pending items per Section 34.2
    unacknowledged_cards = _count_unacknowledged_cards(conn)
    escalation_l2_projects = _count_escalation_l2_projects(conn)
    overdue_followups = _count_overdue_followups(conn)

    alert_items = []
    if unacknowledged_cards:
        alert_items.append(t("inactivity.unacknowledged_cards").format(count=unacknowledged_cards))
    if escalation_l2_projects:
        alert_items.append(t("inactivity.escalation_l2").format(count=escalation_l2_projects))
    if overdue_followups:
        alert_items.append(t("inactivity.overdue_followups").format(count=overdue_followups))

    return InactivityReport(
        is_inactive=is_inactive,
        days_since_last_action=days_since,
        last_action_at=last_action_at,
        unacknowledged_cards=unacknowledged_cards,
        escalation_l2_projects=escalation_l2_projects,
        overdue_followups=overdue_followups,
        dismissed_until=dismissed_until,
        alert_items=alert_items,
    )


def is_alert_dismissed_today(conn: sqlite3.Connection) -> bool:
    """Return True if the inactivity alert is dismissed for today."""
    from src.repositories.operator_state_repo import get_operator_state
    state = get_operator_state(conn)
    dismissed_until = state.get("inactivity_alert_dismissed_until") if state else None
    if not dismissed_until:
        return False
    try:
        dismissed_dt = datetime.fromisoformat(dismissed_until)
        if dismissed_dt.tzinfo is None:
            dismissed_dt = dismissed_dt.replace(tzinfo=timezone.utc)
        return dismissed_dt > datetime.now(timezone.utc)
    except (ValueError, TypeError):
        return False


def format_inactivity_alert(report: InactivityReport) -> str:
    """
    Format inactivity alert per Blueprint Section 34.3.
    Message includes buttons via callback_data in card markup (added by caller).
    """
    last_seen = report.last_action_at[:10] if report.last_action_at else "never"
    lines = [
        t("inactivity.header"),
        "━━━━━━━━━━━━━━━━━━━━━━━",
        t("inactivity.operator_inactive").format(days=report.days_since_last_action),
        t("inactivity.last_seen").format(date=last_seen),
        "",
    ]
    if report.alert_items:
        lines.append(t("inactivity.pending_items"))
        for item in report.alert_items:
            lines.append(f"  • {item}")
    else:
        lines.append(t("inactivity.no_pending"))
    return "\n".join(lines)


def _count_unacknowledged_cards(conn: sqlite3.Connection) -> int:
    """Count ProposedUpdates needing operator review (unacknowledged cards)."""
    row = conn.execute(
        "SELECT COUNT(*) FROM proposed_updates WHERE decision_outcome = 'needs_review'"
    ).fetchone()
    return row[0] if row else 0


def _count_escalation_l2_projects(conn: sqlite3.Connection) -> int:
    """Count projects with at least one escalation_level=2 internal follow-up."""
    row = conn.execute(
        """SELECT COUNT(DISTINCT project_id) FROM internal_followups
           WHERE escalation_level >= 2 AND status = 'open'"""
    ).fetchone()
    return row[0] if row else 0


def _count_overdue_followups(conn: sqlite3.Connection) -> int:
    """Count open internal follow-ups past their due date."""
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    row = conn.execute(
        "SELECT COUNT(*) FROM internal_followups WHERE status = 'open' AND due_date < ?",
        (today,),
    ).fetchone()
    return row[0] if row else 0
