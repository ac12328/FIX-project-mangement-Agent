"""Working memory endpoint."""

import sqlite3

from fastapi import APIRouter, Depends

from src.api.deps import get_conn
from src.context.working_memory_display import get_operator_working_memory

router = APIRouter(tags=["memory"])


@router.get("/projects/{project_id}/memory")
def get_project_memory(
    project_id: str,
    conn: sqlite3.Connection = Depends(get_conn),
):
    """Operator-facing working memory (filters internal keys, enriches staleness)."""
    return get_operator_working_memory(conn, project_id)
