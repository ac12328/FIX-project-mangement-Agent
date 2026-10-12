"""Timeline endpoint for milestone progress, delivery risk, and project health."""

from __future__ import annotations

import re
import sqlite3
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from fastapi import APIRouter, Depends

from config.settings import CONFIG
from src.api.deps import get_read_conn

router = APIRouter(tags=["timeline"])

MILESTONE_ORDER = (
    "M1_NetworkSetup",
    "M2_UATSessionConfiguration",
    "M3_UATEnvironmentAccess",
    "M4_ApplicationTesting",
    "M5_ConformanceTestCases",
    "M6_TestReportAndProductionPreparation",
    "M7_LogReviewAndProductionSessionDelivery",
    "M8_GoLive",
)
MILESTONE_RANK = {milestone: index for index, milestone in enumerate(MILESTONE_ORDER, 1)}
DUE_SOON_DAYS = 14
_ISO_DATE_PATTERN = re.compile(r"\d{4}-\d{2}-\d{2}")


@router.get("/timeline")
def get_timeline(conn: sqlite3.Connection = Depends(get_read_conn)):
    """Return active projects ordered by current milestone and go-live date."""
    return build_timeline(conn)


def build_timeline(conn: sqlite3.Connection) -> list[dict]:
    """Build the timeline read model for legacy and UI v2 endpoints."""
    from config.defaults import ACTIVE_PROJECT_STATUSES

    placeholders = ",".join("?" for _ in ACTIVE_PROJECT_STATUSES)
    rows = conn.execute(
        f"""SELECT project_id, client_name, project_status, current_milestone,
                   ball_with, target_go_live, project_tier
            FROM projects
            WHERE project_status IN ({placeholders})
              AND project_id <> '_system'""",
        ACTIVE_PROJECT_STATUSES,
    ).fetchall()

    today = _local_today()
    result = []
    for record in rows:
        project = dict(record)
        target_date, normalized_target = _parse_target_date(project.get("target_go_live"))
        project["target_go_live"] = normalized_target
        project["health"] = _classify_health(project)
        project["schedule_status"], project["days_to_go_live"] = _classify_schedule(
            target_date,
            normalized_target,
            today=today,
        )
        result.append(project)

    result.sort(key=_timeline_sort_key)
    return result


def _local_today() -> date:
    """Return today's date in the configured working-hours timezone."""
    timezone_name = CONFIG.working_hours.timezone or "Asia/Hong_Kong"
    try:
        local_timezone = ZoneInfo(timezone_name)
    except ZoneInfoNotFoundError:
        try:
            local_timezone = ZoneInfo("Asia/Hong_Kong")
        except ZoneInfoNotFoundError:
            local_timezone = timezone(timedelta(hours=8))
    return datetime.now(local_timezone).date()


def _parse_target_date(value: object) -> tuple[date | None, str | None]:
    """Normalize blank values and parse ISO calendar dates."""
    if value is None:
        return None, None

    normalized = str(value).strip()
    if not normalized:
        return None, None

    if _ISO_DATE_PATTERN.fullmatch(normalized) is None:
        return None, normalized

    try:
        return date.fromisoformat(normalized), normalized
    except ValueError:
        return None, normalized


def _classify_schedule(
    target_date: date | None,
    normalized_target: str | None,
    *,
    today: date,
) -> tuple[str, int | None]:
    """Classify go-live timing independently from operational project health."""
    if normalized_target is None:
        return "no_date", None
    if target_date is None:
        return "invalid", None

    days_to_go_live = (target_date - today).days
    if days_to_go_live < 0:
        return "overdue", days_to_go_live
    if days_to_go_live <= DUE_SOON_DAYS:
        return "due_soon", days_to_go_live
    return "scheduled", days_to_go_live


def _timeline_sort_key(project: dict) -> tuple[int, int, date, str]:
    """Sort M8 to M1, then earliest valid go-live date, then project id."""
    milestone_rank = MILESTONE_RANK.get(project.get("current_milestone"), 0)
    target_date, _ = _parse_target_date(project.get("target_go_live"))
    has_valid_date = target_date is not None
    return (
        -milestone_rank,
        0 if has_valid_date else 1,
        target_date or date.max,
        str(project.get("project_id") or ""),
    )


def _classify_health(project: dict) -> str:
    """Classify operational health without using target go-live dates."""
    if project.get("project_status") == "Blocked":
        return "red"
    if (
        project.get("ball_with") == "External"
        and project.get("project_status") == "Waiting"
    ):
        return "yellow"
    return "green"
