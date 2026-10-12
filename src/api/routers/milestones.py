"""Milestone history endpoint."""

import sqlite3

from fastapi import APIRouter, Depends

from src.api.deps import get_conn
from src.repositories.milestone_history_repo import get_milestone_history

router = APIRouter(tags=["milestones"])


@router.get("/projects/{project_id}/milestones")
def get_project_milestone_history(
    project_id: str,
    conn: sqlite3.Connection = Depends(get_conn),
):
    return get_milestone_history(conn, project_id)
