"""One read transaction for the page, Markdown, and copied Gantt image."""

from __future__ import annotations

import sqlite3
from datetime import date, datetime, time, timedelta, timezone

from src.api.schemas.project_report import ProjectReportItem, ProjectSummaryReport
from src.api.ui_v2_completed import completed_project_rows

# Hong Kong has no daylight-saving transitions in the reporting period.
HKT = timezone(timedelta(hours=8), "Asia/Hong_Kong")


def _timestamp(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
    except (ValueError, TypeError, OverflowError):
        return None


def _target(value: str | None) -> str | None:
    if not value or not value.strip():
        return None
    try:
        return date.fromisoformat(value.strip()).isoformat()
    except ValueError:
        return None


def build_project_summary_report(
    conn: sqlite3.Connection, *, owner: str | None = None, now: datetime | None = None,
) -> ProjectSummaryReport:
    """Freeze current state. No historical as-of reconstruction is implied.

    The completed window is today and the prior 13 HKT calendar days, through
    snapshot time. Archived/cancelled/internal projects are deliberately excluded.
    """
    stamp = now or datetime.now(timezone.utc)
    if stamp.tzinfo is None:
        stamp = stamp.replace(tzinfo=timezone.utc)
    local_day = stamp.astimezone(HKT).date()
    first_day = local_day - timedelta(days=13)
    cutoff = datetime.combine(first_day, time.min, tzinfo=HKT)
    # Preserve any caller-owned transaction (e.g. isolated tests).
    own_transaction = not conn.in_transaction
    if own_transaction:
        conn.execute("BEGIN")
    try:
        rows = conn.execute(
            """SELECT project_id, client_name, market, project_status,
                      current_milestone, ball_with, operator_id, created_at,
                      target_go_live, next_action
               FROM projects WHERE substr(project_id, 1, 1) <> '_'
                 AND project_status IN ('Active', 'Blocked', 'Waiting', 'Completed')
                 AND (? IS NULL OR operator_id = ?)
               ORDER BY lower(COALESCE(client_name, project_id)), project_id""",
            (owner, owner),
        ).fetchall()
        completion_dates = {
            str(row["project_id"]): _timestamp(row["completed_at"])
            for row in completed_project_rows(conn)
        }
    finally:
        if own_transaction:
            conn.rollback()

    active: list[ProjectReportItem] = []
    postponed: list[ProjectReportItem] = []
    completed: list[ProjectReportItem] = []
    undated = 0
    for row in rows:
        status = row["project_status"]
        completion = completion_dates.get(row["project_id"])
        if status == "Completed":
            if completion is None or completion > stamp:
                undated += 1
                continue
            if completion < cutoff:
                continue
        notes: list[str] = []
        created = _timestamp(row["created_at"])
        tracked = created.astimezone(HKT).date().isoformat() if created else None
        if not created or created > stamp:
            tracked = None
            notes.append("Tracked-since date missing or invalid; no start inferred.")
        target = _target(row["target_go_live"])
        if row["target_go_live"] and row["target_go_live"].strip() and not target:
            notes.append("Stored target date is invalid; shown as TBD.")
        if target and tracked and target < tracked:
            notes.append("Target predates tracking; confirm the stored dates.")
        item = ProjectReportItem(
            project_id=row["project_id"], client_name=row["client_name"], market=row["market"],
            project_status=status, current_milestone=row["current_milestone"],
            ball_with=row["ball_with"], operator_id=row["operator_id"],
            tracked_since=tracked, target_go_live=target, next_action=row["next_action"],
            completed_at=completion.astimezone(timezone.utc).isoformat()
            if status == "Completed" and completion else None,
            data_notes=notes,
        )
        if status == "Waiting":
            postponed.append(item)
        elif status == "Completed":
            completed.append(item)
        else:
            active.append(item)
    completed.sort(key=lambda item: item.completed_at or "", reverse=True)
    warnings = []
    if undated:
        warnings.append(
            f"{undated} currently Completed project(s) have no valid accepted completion "
            "date at snapshot time and cannot be placed in the 14-day window."
        )
    return ProjectSummaryReport(
        generated_at=stamp.astimezone(timezone.utc).isoformat(),
        report_date=local_day.isoformat(), completed_from=first_day.isoformat(),
        completed_through=local_day.isoformat(), scope="mine" if owner is not None else "all",
        active=active, postponed=postponed, completed=completed, warnings=warnings,
    )
