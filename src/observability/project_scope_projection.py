"""Project attribution for terminal trace events, without a schema migration."""

from __future__ import annotations

import json
import sqlite3
from typing import Any


def finish_project_scope(
    conn: sqlite3.Connection, run_id: str, metadata: dict[str, Any]
) -> None:
    row = conn.execute(
        "SELECT project_id, metadata_json FROM trace_runs WHERE run_id=?", (run_id,)
    ).fetchone()
    if row is None:
        return
    stored = json.loads(row[1] or "{}")
    stored.setdefault("initial_project_id", row[0])
    project_id = metadata.get("project_id", row[0])
    # A portfolio run can have several projects; never attribute it to just one.
    project_ids = metadata.get("project_ids", metadata.get("scope_project_ids"))
    if project_ids is None and "project_id" not in metadata:
        project_ids = stored.get("project_ids", stored.get("scope_project_ids"))
    if isinstance(project_ids, list):
        project_ids = list(
            dict.fromkeys(pid for pid in project_ids if isinstance(pid, str) and pid)
        )
        project_id = project_ids[0] if len(project_ids) == 1 else None
    else:
        project_ids = [project_id] if project_id else []
    stored.update(effective_project_id=project_id, effective_project_ids=project_ids)
    conn.execute(
        "UPDATE trace_runs SET project_id=?, metadata_json=? WHERE run_id=?",
        (project_id, json.dumps(stored, ensure_ascii=False), run_id),
    )
