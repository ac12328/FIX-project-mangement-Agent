"""
SLA / Deadline Monitoring (Blueprint Section 16 / Plan Step 3.3).
Deterministic daily job — no LLM. Checks milestone deadlines,
follow-up due dates, and project deadlines.

Alert levels: ADVISORY (T-3), URGENT (T-1), DUE_TODAY (T=0), OVERDUE (T+1).
"""

import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timezone


@dataclass
class SLAAlert:
    """A single SLA/deadline alert."""
    project_id: str
    alert_type: str  # milestone_due | followup_due | project_deadline
    alert_level: str  # advisory | urgent | due_today | overdue
    description: str
    due_date: str
    days_remaining: int
    related_id: str | None = None  # followup_id, etc.


@dataclass
class SLAReport:
    """Daily SLA monitoring report."""
    generated_at: str
    total_alerts: int = 0
    overdue: list[SLAAlert] = field(default_factory=list)
    due_today: list[SLAAlert] = field(default_factory=list)
    urgent: list[SLAAlert] = field(default_factory=list)
    advisory: list[SLAAlert] = field(default_factory=list)


def run_sla_check(conn: sqlite3.Connection) -> SLAReport:
    """
    Run the daily SLA/deadline check. Deterministic — no LLM.
    Checks:
    1. Project target_go_live dates
    2. Internal follow-up due dates
    3. SLA limits on projects
    """
    now = datetime.now(timezone.utc)
    report = SLAReport(generated_at=now.isoformat())

    # Check project deadlines
    _check_project_deadlines(conn, now, report)

    # Check follow-up due dates
    _check_followup_deadlines(conn, now, report)

    # Check per-milestone SLA limits
    _check_sla_limits(conn, now, report)

    report.total_alerts = (
        len(report.overdue) + len(report.due_today) +
        len(report.urgent) + len(report.advisory)
    )
    return report


def _check_project_deadlines(
    conn: sqlite3.Connection, now: datetime, report: SLAReport
) -> None:
    """Check target_go_live dates for active projects."""
    rows = conn.execute(
        """SELECT project_id, client_name, target_go_live
           FROM projects
           WHERE project_status IN ('Active', 'Blocked')
           AND target_go_live IS NOT NULL""",
    ).fetchall()
    # Note: 'Waiting' (ball_with=External, client postponed) is excluded —
    # the deadline urgency is with the client, not internal.

    for row in rows:
        d = dict(row)
        # Skip project if operator snoozed the SLA alert and the snooze is still active.
        snooze_row = conn.execute(
            "SELECT memory_value FROM working_memory WHERE project_id = ? AND memory_key = 'sla_snoozed_until'",
            (d["project_id"],),
        ).fetchone()
        if snooze_row:
            try:
                snoozed_until = datetime.fromisoformat(snooze_row["memory_value"])
                if snoozed_until.tzinfo is None:
                    snoozed_until = snoozed_until.replace(tzinfo=timezone.utc)
                if now < snoozed_until:
                    continue
            except (ValueError, TypeError):
                pass

        days = _days_between(now, d["target_go_live"])
        if days is None:
            continue

        alert = SLAAlert(
            project_id=d["project_id"],
            alert_type="project_deadline",
            alert_level=_classify_alert(days),
            description=f"Go-live deadline for {d.get('client_name', d['project_id'])}",
            due_date=d["target_go_live"],
            days_remaining=days,
        )
        _add_to_report(report, alert)


def _check_followup_deadlines(
    conn: sqlite3.Connection, now: datetime, report: SLAReport
) -> None:
    """Check due dates for open internal follow-ups."""
    rows = conn.execute(
        """SELECT followup_id, project_id, description, due_date
           FROM internal_followups
           WHERE status = 'open' AND due_date IS NOT NULL""",
    ).fetchall()

    for row in rows:
        d = dict(row)
        days = _days_between(now, d["due_date"])
        if days is None:
            continue

        alert = SLAAlert(
            project_id=d["project_id"],
            alert_type="followup_due",
            alert_level=_classify_alert(days),
            description=d["description"],
            due_date=d["due_date"],
            days_remaining=days,
            related_id=d["followup_id"],
        )
        _add_to_report(report, alert)


def _check_sla_limits(
    conn: sqlite3.Connection, now: datetime, report: SLAReport,
) -> None:
    """Check per-milestone SLA limits for active projects."""
    from src.utils.calendar import business_days_between

    rows = conn.execute(
        """SELECT p.project_id, p.client_name, p.sla_limit, p.current_milestone,
                  mh.changed_at
           FROM projects p
           LEFT JOIN (
               SELECT project_id, MAX(changed_at) AS changed_at
               FROM milestone_history GROUP BY project_id
           ) mh ON p.project_id = mh.project_id
           WHERE p.project_status IN ('Active', 'Blocked')
           AND p.sla_limit IS NOT NULL AND p.sla_limit > 0
           AND mh.changed_at IS NOT NULL""",
    ).fetchall()

    for row in rows:
        d = dict(row)
        try:
            changed = datetime.fromisoformat(d["changed_at"])
            elapsed = business_days_between(changed.date(), now.date())
            remaining = int(d["sla_limit"]) - elapsed
        except (ValueError, TypeError):
            continue

        if remaining > 2:
            continue

        alert = SLAAlert(
            project_id=d["project_id"],
            alert_type="sla_limit",
            alert_level="overdue" if remaining < 0 else ("due_today" if remaining == 0 else "urgent"),
            description=(
                f"SLA limit {int(d['sla_limit'])}d at "
                f"{d.get('current_milestone', '?')} for {d.get('client_name', d['project_id'])}"
            ),
            due_date=d["changed_at"],
            days_remaining=remaining,
        )
        _add_to_report(report, alert)


def _classify_alert(days_remaining: int) -> str:
    """Classify alert level based on days remaining."""
    if days_remaining < 0:
        return "overdue"
    elif days_remaining == 0:
        return "due_today"
    elif days_remaining <= 1:
        return "urgent"
    elif days_remaining <= 3:
        return "advisory"
    return "advisory"  # Only include T-3 or less


def _add_to_report(report: SLAReport, alert: SLAAlert) -> None:
    """Add an alert to the appropriate list in the report."""
    if alert.days_remaining > 3:
        return  # Don't alert for things more than 3 days out
    if alert.alert_level == "overdue":
        report.overdue.append(alert)
    elif alert.alert_level == "due_today":
        report.due_today.append(alert)
    elif alert.alert_level == "urgent":
        report.urgent.append(alert)
    else:
        report.advisory.append(alert)


def _days_between(now: datetime, date_str: str) -> int | None:
    """Calculate days between now and a date string. Negative = overdue."""
    try:
        target = datetime.fromisoformat(date_str)
        if target.tzinfo is None:
            target = target.replace(tzinfo=timezone.utc)
        return (target - now).days
    except (ValueError, TypeError):
        return None


def format_sla_report(report: SLAReport) -> str:
    """Format SLA report as text."""
    if report.total_alerts == 0:
        return "SLA Check — No alerts. All deadlines on track."

    lines = [f"SLA Check — {report.total_alerts} alert(s)", ""]

    for label, items in [
        ("OVERDUE", report.overdue),
        ("DUE TODAY", report.due_today),
        ("URGENT (T-1)", report.urgent),
        ("ADVISORY (T-3)", report.advisory),
    ]:
        if items:
            lines.append(f"{label}:")
            for a in items:
                lines.append(f"  {a.project_id}: {a.description} (due: {a.due_date})")
            lines.append("")

    return "\n".join(lines)
