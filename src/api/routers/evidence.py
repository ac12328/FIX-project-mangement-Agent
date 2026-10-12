"""Evidence endpoints for a project."""

import sqlite3

from fastapi import APIRouter, Depends, Query

from src.api.deps import get_conn
from src.repositories.evidence_repo import get_recent_evidence

router = APIRouter(tags=["evidence"])


@router.get("/projects/{project_id}/evidence")
def get_project_evidence(
    project_id: str,
    limit: int = Query(20, ge=1, le=100),
    conn: sqlite3.Connection = Depends(get_conn),
):
    items = get_recent_evidence(conn, project_id, limit)
    return [e.model_dump() for e in items]
