"""
Periodic Reports (Blueprint Section 33 / Plan Step 3.14).
Weekly (Friday 5pm) + Monthly (1st of month). Deterministic — no LLM.
Output: Telegram push + Dashboard + exportable markdown.
"""

import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timezone

from src.telegram.i18n import t


@dataclass
class ProjectSnapshot:
    """Snapshot of a project for reporting."""
    project_id: str
    client_name: str | None
    current_milestone: str | None
    project_status: str
    ball_with: str | None
    days_active: int | None = None


@dataclass
class PeriodicReport:
    """A periodic (weekly/monthly) management report."""
    report_type: str  # weekly | monthly
    generated_at: str
    period_start: str
    period_end: str
    # Summary stats
    total_active: int = 0
    total_completed_in_period: int = 0
    total_new_in_period: int = 0
    total_blocked: int = 0
    # Details
    milestone_distribution: dict[str, int] = field(default_factory=dict)
    project_snapshots: list[ProjectSnapshot] = field(default_factory=list)
    highlights: list[str] = field(default_factory=list)
    # Velocity metrics (WP-S4.1)
    milestone_transitions_in_period: int = 0
    avg_days_per_milestone: float | None = None
    avg_days_to_complete: float | None = None
    # Trend comparison (WP-S4.1)
    prior_period_active: int | None = None
    prior_period_completed: int | None = None


def generate_weekly_report(
    conn: sqlite3.Connection,
    period_start: str,
    period_end: str,
) -> PeriodicReport:
    """Generate a weekly management report. Deterministic — no LLM."""
    return _generate_report(conn, "weekly", period_start, period_end)


def generate_monthly_report(
    conn: sqlite3.Connection,
    period_start: str,
    period_end: str,
) -> PeriodicReport:
    """Generate a monthly management report. Deterministic — no LLM."""
    return _generate_report(conn, "monthly", period_start, period_end)


def _generate_report(
    conn: sqlite3.Connection,
    report_type: str,
    period_start: str,
    period_end: str,
) -> PeriodicReport:
    """Generate a periodic report."""
    now = datetime.now(timezone.utc)
    report = PeriodicReport(
        report_type=report_type,
        generated_at=now.isoformat(),
        period_start=period_start,
        period_end=period_end,
    )

    # Active projects
    active = conn.execute(
        "SELECT * FROM projects WHERE project_id NOT GLOB '_*' AND project_status IN ('Active', 'Blocked', 'Waiting')"
    ).fetchall()
    report.total_active = len(active)

    # Blocked count
    report.total_blocked = sum(1 for r in active if dict(r)["project_status"] == "Blocked")

    # Projects completed in period
    completed = conn.execute(
        """SELECT COUNT(*) FROM projects
           WHERE project_id NOT GLOB '_*' AND project_status = 'Completed'
           AND updated_at BETWEEN ? AND ?""",
        (period_start, period_end),
    ).fetchone()
    report.total_completed_in_period = completed[0] if completed else 0

    # New projects in period
    new = conn.execute(
        """SELECT COUNT(*) FROM projects
           WHERE project_id NOT GLOB '_*' AND created_at BETWEEN ? AND ?""",
        (period_start, period_end),
    ).fetchone()
    report.total_new_in_period = new[0] if new else 0

    # Milestone distribution
    for row in active:
        d = dict(row)
        ms = d.get("current_milestone") or "Not set"
        report.milestone_distribution[ms] = report.milestone_distribution.get(ms, 0) + 1

    # Project snapshots
    for row in active:
        d = dict(row)
        days_active = None
        if d.get("created_at"):
            try:
                created = datetime.fromisoformat(d["created_at"])
                if created.tzinfo is None:
                    created = created.replace(tzinfo=timezone.utc)
                days_active = (now - created).days
            except (ValueError, TypeError):
                pass

        report.project_snapshots.append(ProjectSnapshot(
            project_id=d["project_id"],
            client_name=d.get("client_name"),
            current_milestone=d.get("current_milestone"),
            project_status=d["project_status"],
            ball_with=d.get("ball_with"),
            days_active=days_active,
        ))

    # Highlights
    if report.total_completed_in_period > 0:
        report.highlights.append(t("report.highlight_completed").format(count=report.total_completed_in_period))
    if report.total_new_in_period > 0:
        report.highlights.append(t("report.highlight_started").format(count=report.total_new_in_period))
    if report.total_blocked > 0:
        report.highlights.append(t("report.highlight_blocked").format(count=report.total_blocked))

    # Velocity: milestone transitions in period
    transitions = conn.execute(
        "SELECT COUNT(*) FROM milestone_history WHERE changed_at BETWEEN ? AND ?",
        (period_start, period_end),
    ).fetchone()
    report.milestone_transitions_in_period = transitions[0] if transitions else 0

    if report.milestone_transitions_in_period > 0:
        # Average days per milestone transition
        avg_row = conn.execute(
            """SELECT AVG(julianday(mh.changed_at) - julianday(prev.changed_at))
               FROM milestone_history mh
               LEFT JOIN (
                   SELECT project_id, changed_at,
                          ROW_NUMBER() OVER (PARTITION BY project_id ORDER BY changed_at DESC) as rn
                   FROM milestone_history
               ) prev ON mh.project_id = prev.project_id AND prev.rn = 2
               WHERE mh.changed_at BETWEEN ? AND ?
               AND prev.changed_at IS NOT NULL""",
            (period_start, period_end),
        ).fetchone()
        if avg_row and avg_row[0] is not None:
            report.avg_days_per_milestone = round(avg_row[0], 1)

    # Average days to complete (for projects completed in period)
    if report.total_completed_in_period > 0:
        avg_complete = conn.execute(
            """SELECT AVG(julianday(updated_at) - julianday(created_at))
               FROM projects
               WHERE project_id NOT GLOB '_*' AND project_status = 'Completed'
               AND updated_at BETWEEN ? AND ?""",
            (period_start, period_end),
        ).fetchone()
        if avg_complete and avg_complete[0] is not None:
            report.avg_days_to_complete = round(avg_complete[0], 1)

    # Trend comparison: compute prior period stats
    from datetime import timedelta
    try:
        ps = datetime.fromisoformat(period_start)
        pe = datetime.fromisoformat(period_end)
        delta = pe - ps
        prior_start = (ps - delta).isoformat()
        prior_end = ps.isoformat()
        # Prior active: count projects that were active during prior period
        prior_active_row = conn.execute(
            """SELECT COUNT(*) FROM projects
               WHERE created_at < ? AND (project_status IN ('Active','Blocked','Waiting')
               OR updated_at > ?)""",
            (prior_end, prior_start),
        ).fetchone()
        report.prior_period_active = prior_active_row[0] if prior_active_row else None
        prior_completed_row = conn.execute(
            """SELECT COUNT(*) FROM projects
               WHERE project_id NOT GLOB '_*' AND project_status = 'Completed' AND updated_at BETWEEN ? AND ?""",
            (prior_start, prior_end),
        ).fetchone()
        report.prior_period_completed = prior_completed_row[0] if prior_completed_row else None
    except (ValueError, TypeError):
        pass

    return report


def format_report_markdown(report: PeriodicReport) -> str:
    """Format report as markdown for Telegram/export."""
    lines = [
        t("report.header").format(report_type=report.report_type.title()),
        "━━━━━━━━━━━━━━━━━━━━━━━",
        t("report.period").format(start=report.period_start, end=report.period_end),
        "",
        t("report.summary").format(active=report.total_active, new=report.total_new_in_period, completed=report.total_completed_in_period, blocked=report.total_blocked),
        "",
    ]

    if report.highlights:
        lines.append(t("report.highlights"))
        for h in report.highlights:
            lines.append(f"  • {h}")
        lines.append("")

    if report.milestone_distribution:
        lines.append(t("report.milestone_dist"))
        for ms, count in sorted(report.milestone_distribution.items()):
            lines.append(f"  {ms}: {count}")
        lines.append("")

    if report.project_snapshots:
        lines.append(t("report.projects").format(count=len(report.project_snapshots)))
        for snap in report.project_snapshots:
            ball = f" · {snap.ball_with}" if snap.ball_with else ""
            milestone = f" · {snap.current_milestone}" if snap.current_milestone else ""
            lines.append(
                f"  {snap.project_id} — {snap.client_name or '-'}{milestone}{ball} [{snap.project_status}]"
            )
        lines.append("")

    # Velocity section
    velocity_parts = []
    if report.milestone_transitions_in_period > 0:
        velocity_parts.append(f"Milestone transitions: {report.milestone_transitions_in_period}")
    if report.avg_days_per_milestone is not None:
        velocity_parts.append(f"Avg days/milestone: {report.avg_days_per_milestone}")
    if report.avg_days_to_complete is not None:
        velocity_parts.append(f"Avg days to complete: {report.avg_days_to_complete}")
    if velocity_parts:
        lines.append("Velocity:")
        for v in velocity_parts:
            lines.append(f"  {v}")
        lines.append("")

    # Trend comparison
    trend_parts = []
    if report.prior_period_active is not None:
        delta = report.total_active - report.prior_period_active
        arrow = "+" if delta > 0 else ""
        trend_parts.append(f"Active: {report.total_active} ({arrow}{delta} vs prior)")
    if report.prior_period_completed is not None:
        delta = report.total_completed_in_period - report.prior_period_completed
        arrow = "+" if delta > 0 else ""
        trend_parts.append(f"Completed: {report.total_completed_in_period} ({arrow}{delta} vs prior)")
    if trend_parts:
        lines.append("Trend:")
        for tr in trend_parts:
            lines.append(f"  {tr}")

    return "\n".join(lines)
