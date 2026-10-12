"""
Layer 1: Project Truth — loads canonical project state from DB.
"""

import sqlite3

from src.models.project import Project
from src.repositories.project_repo import get_project


def load_project_truth(
    conn: sqlite3.Connection, project_id: str
) -> tuple[Project | None, list[str]]:
    """
    Load project truth.
    Returns (project, missing_flags).
    """
    project = get_project(conn, project_id)
    missing = []
    if project is None:
        missing.append("project_not_found")
        return None, missing
    if project.current_milestone is None:
        missing.append("no_milestone_set")
    if project.next_action is None:
        missing.append("no_next_action")
    return project, missing
