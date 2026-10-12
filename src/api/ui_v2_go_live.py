"""Source-labelled go-live display dates without rewriting historical truth.

Confirmed actual dates win. For completed M8 projects only, fall back to the
first valid recorded entry into M8 (including evidence-backed forward jumps).
The inferred day is the UTC calendar date of that audit event, not a claim about
actual production activation. Targets, closure dates and updated_at are unused.
"""
from __future__ import annotations

import sqlite3
from datetime import datetime, timezone

from src.api.schemas.ui_v2 import GoLiveDateReadModel


def project_go_live_dates(
    conn: sqlite3.Connection, projects: list[sqlite3.Row],
) -> dict[str, GoLiveDateReadModel]:
    dates: dict[str, GoLiveDateReadModel] = {}
    pending: list[str] = []
    for project in projects:
        project_id = str(project["project_id"])
        if project["actual_go_live"]:
            dates[project_id] = GoLiveDateReadModel(
                go_live_date=project["actual_go_live"], go_live_date_source="confirmed",
                go_live_date_source_ref=project["actual_go_live_evidence_id"],
                go_live_date_source_note=project["actual_go_live_source_quote"],
            )
        else:
            dates[project_id] = GoLiveDateReadModel()
            if (project["project_status"] == "Completed"
                    and project["current_milestone"] == "M8_GoLive"):
                pending.append(project_id)

    now = datetime.now(timezone.utc)
    earliest: dict[str, tuple[datetime, int]] = {}
    # Bound SQLite placeholders for larger portfolios; no per-project queries.
    for start in range(0, len(pending), 400):
        batch = pending[start:start + 400]
        placeholders = ",".join("?" for _ in batch)
        rows = conn.execute(
            f"""SELECT id, project_id, changed_at, reason FROM milestone_history
                WHERE project_id IN ({placeholders}) AND to_milestone='M8_GoLive'
                  AND from_milestone IS NOT 'M8_GoLive'""", batch,
        ).fetchall()
        for row in rows:
            try:
                stamp = datetime.fromisoformat(str(row["changed_at"]).replace("Z", "+00:00"))
                stamp = (stamp if stamp.tzinfo else stamp.replace(tzinfo=timezone.utc))
                stamp = stamp.astimezone(timezone.utc)
            except (ValueError, TypeError, OverflowError):
                continue
            if stamp > now:
                continue
            project_id = str(row["project_id"])
            key = (stamp, int(row["id"]))
            if project_id in earliest and earliest[project_id] <= key:
                continue
            earliest[project_id] = key
            dates[project_id] = GoLiveDateReadModel(
                go_live_date=stamp.date().isoformat(), go_live_date_source="m8_inferred",
                go_live_date_source_ref=f"milestone_history:{row['id']}",
                go_live_date_source_at=stamp.isoformat(),
                go_live_date_source_note=row["reason"],
            )
    return dates
