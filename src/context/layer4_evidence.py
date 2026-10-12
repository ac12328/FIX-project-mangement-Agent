"""
Layer 4: Evidence — loads recent evidence for context.
"""

import sqlite3

from src.models.evidence import Evidence
from src.repositories.evidence_repo import get_recent_evidence


def load_evidence(
    conn: sqlite3.Connection, project_id: str, limit: int = 20
) -> tuple[list[Evidence], list[str]]:
    """
    Load recent evidence for a project.
    Returns (evidence_list, missing_flags).
    """
    evidence = get_recent_evidence(conn, project_id, limit=limit)
    missing = []
    if not evidence:
        missing.append("no_evidence")
    return evidence, missing
