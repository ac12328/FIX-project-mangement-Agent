"""Activity timeline endpoints."""

import sqlite3

from fastapi import APIRouter, Depends, Query

from src.api.deps import get_read_conn
from src.repositories.activity_log_repo import (
    get_distinct_action_types,
    get_filtered_activity,
    get_recent_activity,
)

router = APIRouter(tags=["activity"])


@router.get("/activity")
def get_all_activity(
    limit: int = Query(50, le=100),
    offset: int = Query(0, ge=0),
    actor: str | None = Query(None),
    action_type: str | None = Query(None),
    project_id: str | None = Query(None),
    since: str | None = Query(None),
    until: str | None = Query(None),
    conn: sqlite3.Connection = Depends(get_read_conn),
):
    """Recent activity across all projects, with optional filters."""
    rows = get_filtered_activity(
        conn,
        actor=actor,
        action_type=action_type,
        project_id=project_id,
        since=since,
        until=until,
        limit=limit + 1,
        offset=offset,
    )
    has_more = len(rows) > limit
    return {"items": rows[:limit], "has_more": has_more}


@router.get("/activity/action-types")
def get_action_types(
    conn: sqlite3.Connection = Depends(get_read_conn),
):
    """Distinct action_type values for filter dropdowns."""
    return get_distinct_action_types(conn)


@router.get("/projects/{project_id}/activity")
def get_project_activity(
    project_id: str,
    limit: int = Query(50, le=200),
    conn: sqlite3.Connection = Depends(get_read_conn),
):
    return get_recent_activity(conn, project_id, limit)
