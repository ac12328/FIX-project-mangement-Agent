"""
Portfolio Mode (Blueprint Section 15 / Plan Step 3.2).
On-demand or scheduled scan of all active projects with priority ranking.
READ ONLY — never modifies project truth.
"""

import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timezone

from src.models.project import Project
from src.portfolio.email_thread_state import count_unanswered_threads
from src.repositories.project_repo import get_active_projects
from src.telegram.i18n import t


@dataclass
class PortfolioItem:
    """A single project in the portfolio view."""
    project_id: str
    client_name: str | None
    current_milestone: str | None
    ball_with: str | None
    next_action: str | None
    project_status: str
    priority_score: float = 0.0
    flags: list[str] = field(default_factory=list)
    days_inactive: int | None = None


@dataclass
class PortfolioSummary:
    """Portfolio scan results."""
    generated_at: str
    total_active: int
    blocked_count: int = 0
    waiting_count: int = 0
    items: list[PortfolioItem] = field(default_factory=list)


def scan_portfolio(
    conn: sqlite3.Connection, *, now: datetime | None = None
) -> PortfolioSummary:
    """
    Scan all active projects and rank by priority.
    READ ONLY — never modifies project truth.
    """
    projects = get_active_projects(conn)
    now = now or datetime.now(timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)

    summary = PortfolioSummary(
        generated_at=now.isoformat(),
        total_active=len(projects),
    )

    for project in projects:
        item = _assess_project(conn, project, now)
        summary.items.append(item)
        if project.project_status == "Blocked":
            summary.blocked_count += 1
        elif project.project_status == "Waiting":
            summary.waiting_count += 1

    # Sort by priority score descending (highest priority first)
    summary.items.sort(key=lambda x: x.priority_score, reverse=True)

    return summary


def _assess_project(
    conn: sqlite3.Connection, project: Project, now: datetime
) -> PortfolioItem:
    """Assess a project and assign priority score."""
    flags = []
    score = 0.0

    # Days since last activity
    days_inactive = None
    if project.last_activity_at:
        last = project.last_activity_at
        if last.tzinfo is None:
            last = last.replace(tzinfo=timezone.utc)
        days_inactive = (now - last).days

        if days_inactive >= 7:
            score += 30
            flags.append(f"stale ({days_inactive}d)")
        elif days_inactive >= 3:
            score += 15
            flags.append(f"aging ({days_inactive}d)")

    # Blocked projects get higher priority
    if project.project_status == "Blocked":
        score += 25
        flags.append("blocked")

    # No next action defined
    if not project.next_action:
        score += 10
        flags.append("no_next_action")

    # Unanswered messages
    unanswered = _count_unanswered(conn, project.project_id)
    if unanswered > 0:
        score += min(unanswered * 5, 20)
        flags.append(f"{unanswered} unanswered")

    # Approaching deadline
    if project.target_go_live:
        days_to = _days_until(project.target_go_live, now)
        if days_to is not None:
            if days_to < 0:
                score += 40
                flags.append(f"overdue ({abs(days_to)}d)")
            elif days_to <= 7:
                score += 20
                flags.append(f"deadline in {days_to}d")

    # Open follow-ups
    open_fups = conn.execute(
        "SELECT COUNT(*) FROM internal_followups WHERE project_id = ? AND status = 'open'",
        (project.project_id,),
    ).fetchone()[0]
    if open_fups > 0:
        score += min(open_fups * 3, 15)
        flags.append(f"{open_fups} follow-up(s)")

    if not flags:
        flags.append("on_track")

    return PortfolioItem(
        project_id=project.project_id,
        client_name=project.client_name,
        current_milestone=project.current_milestone.value if project.current_milestone else None,
        ball_with=project.ball_with.value if project.ball_with else None,
        next_action=project.next_action,
        project_status=project.project_status.value if hasattr(project.project_status, 'value') else str(project.project_status),
        priority_score=score,
        flags=flags,
        days_inactive=days_inactive,
    )


def format_portfolio_text(summary: PortfolioSummary) -> str:
    """Format portfolio summary as text."""
    # Count by health tier
    red_count = sum(1 for it in summary.items if it.priority_score >= 50)
    yellow_count = sum(1 for it in summary.items if 20 <= it.priority_score < 50)
    green_count = sum(1 for it in summary.items if it.priority_score < 20)
    lines = [
        t("portfolio.header"),
        "━━━━━━━━━━━━━━━━━━━━━━━",
        t("portfolio.summary").format(red=red_count, yellow=yellow_count, green=green_count, total=summary.total_active),
        "",
    ]

    # Group items by health tier
    red_items = [it for it in summary.items if it.priority_score >= 50]
    yellow_items = [it for it in summary.items if 20 <= it.priority_score < 50]
    green_items = [it for it in summary.items if it.priority_score < 20]

    def _format_item(item: PortfolioItem) -> list[str]:
        if item.priority_score >= 50:
            dot = "🔴"
        elif item.priority_score >= 20:
            dot = "🟡"
        else:
            dot = "🟢"
        ball = f" · {item.ball_with}" if item.ball_with else ""
        milestone = f" · {item.current_milestone}" if item.current_milestone else ""
        flags = ", ".join(item.flags) if item.flags else ""
        return [
            f"┌ {dot} **{item.project_id}**{milestone}{ball}",
            f"│  {t('portfolio.score')}: {item.priority_score:.0f} · {flags}",
        ]

    if red_items:
        lines.append(t("portfolio.needs_attention").format(count=len(red_items)))
        for item in red_items:
            lines.extend(_format_item(item))
        lines.append("")

    if yellow_items:
        lines.append(t("portfolio.monitor").format(count=len(yellow_items)))
        for item in yellow_items:
            lines.extend(_format_item(item))
        lines.append("")

    if green_items:
        lines.append(t("portfolio.on_track").format(count=len(green_items)))
        for item in green_items:
            lines.extend(_format_item(item))
        lines.append("")

    if not summary.items:
        lines.append(t("portfolio.all_normal"))
        lines.append("")

    lines.append("")
    lines.append(t("portfolio.hint"))
    return "\n".join(lines)


def _count_unanswered(conn: sqlite3.Connection, project_id: str) -> int:
    """Compatibility wrapper around the shared processed-email thread resolver."""
    return count_unanswered_threads(conn, project_id)


def _days_until(date_str: str, now: datetime) -> int | None:
    try:
        target = datetime.fromisoformat(date_str)
        if target.tzinfo is None:
            target = target.replace(tzinfo=timezone.utc)
        return (target - now).days
    except (ValueError, TypeError):
        return None
