"""Reports endpoint — weekly and monthly portfolio reports."""

import sqlite3
from dataclasses import asdict
from datetime import datetime, timedelta, timezone
from typing import Annotated

from fastapi import APIRouter, Depends, Query

from src.api.auth import CurrentActor
from src.api.deps import get_conn, get_current_actor, get_read_conn
from src.api.project_report import build_project_summary_report
from src.api.schemas.project_report import ProjectSummaryReport
from src.portfolio.periodic_reports import (
    format_report_markdown,
    generate_monthly_report,
    generate_weekly_report,
)

router = APIRouter(tags=["reports"])


@router.get("/reports")
def get_report(
    type: str = Query("weekly", pattern="^(weekly|monthly)$"),
    date: str | None = Query(None, description="ISO date for period end (YYYY-MM-DD); defaults to today"),
    conn: sqlite3.Connection = Depends(get_conn),
):
    """Generate a portfolio report for the specified period."""
    now = datetime.now(timezone.utc)
    end_date = date or now.strftime("%Y-%m-%d")
    try:
        end_dt = datetime.strptime(end_date, "%Y-%m-%d")
    except ValueError:
        end_dt = now

    if type == "monthly":
        start_date = end_dt.replace(day=1).strftime("%Y-%m-%d")
        report = generate_monthly_report(conn, start_date, end_date)
    else:
        start_date = (end_dt - timedelta(days=7)).strftime("%Y-%m-%d")
        report = generate_weekly_report(conn, start_date, end_date)

    snapshots = [asdict(s) for s in report.project_snapshots]
    return {
        "report_type": report.report_type,
        "period_start": report.period_start,
        "period_end": report.period_end,
        "snapshots": snapshots,
        "highlights": report.highlights,
        "milestone_distribution": report.milestone_distribution,
        "markdown": format_report_markdown(report),
    }


@router.get("/reports/project-summary", response_model=ProjectSummaryReport)
def get_project_summary_report(
    actor: Annotated[CurrentActor, Depends(get_current_actor)],
    conn: Annotated[sqlite3.Connection, Depends(get_read_conn)],
    mine: bool = False,
) -> ProjectSummaryReport:
    """All matching projects, without pagination, under the shared-read policy."""
    return build_project_summary_report(conn, owner=actor.user_id if mine else None)
