"""Completed-project list for Operations, dated from accepted status audits."""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta, timezone
from typing import Any

from src.api.schemas.ui_v2 import CompletedProjectListItemReadModel, FacetReadModel
from src.api.ui_v2_common import _age_days, _facets

_SYSTEM_PROJECT_PREFIX = "\\_%"
COMPLETED_STATUS = "Completed"
ALLOWED_WINDOWS = frozenset({14, 30})


def _is_completed_change(change: Any) -> bool:
    if not isinstance(change, dict):
        return False
    return (
        str(change.get("field") or "") == "project_status"
        and str(change.get("new_value") or "") == COMPLETED_STATUS
    )


def completed_project_rows(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    """Current Completed projects with their latest accepted completion audit.

    Shared by the Operations list and the reporting snapshot. Never substitute
    target, updated_at, or inferred go-live dates for an accepted completion.
    """
    rows = conn.execute(
        """SELECT p.project_id, p.client_name, p.market, p.project_status,
                  p.ball_with, p.current_milestone, p.operator_id,
                  pu.update_id, pu.proposed_changes,
                  COALESCE(pu.decided_at, pu.created_at) AS completed_at
           FROM projects p
           JOIN proposed_updates pu ON pu.project_id = p.project_id
           WHERE p.project_status = ?
             AND p.project_id NOT LIKE ? ESCAPE char(92)
             AND pu.decision_outcome = 'accepted'
           ORDER BY datetime(COALESCE(pu.decided_at, pu.created_at)) DESC,
                    pu.rowid DESC""",
        (COMPLETED_STATUS, _SYSTEM_PROJECT_PREFIX),
    ).fetchall()

    seen: set[str] = set()
    result: list[sqlite3.Row] = []
    for row in rows:
        project_id = str(row["project_id"])
        if project_id in seen:
            continue
        try:
            changes = json.loads(row["proposed_changes"] or "[]")
        except (TypeError, json.JSONDecodeError):
            continue
        if not isinstance(changes, list) or not any(_is_completed_change(item) for item in changes):
            continue
        seen.add(project_id)
        result.append(row)
    return result


def list_completed_projects(
    conn: sqlite3.Connection,
    *,
    days: int,
    q: str | None,
) -> tuple[list[CompletedProjectListItemReadModel], list[FacetReadModel]]:
    if days not in ALLOWED_WINDOWS:
        raise ValueError("completed_within_days must be 14 or 30")
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    items: list[CompletedProjectListItemReadModel] = []
    needle = (q or "").strip().casefold()
    for row in completed_project_rows(conn):
        project_id = str(row["project_id"])
        completed_at = str(row["completed_at"] or "")
        completed_dt = None
        raw = completed_at.replace("Z", "+00:00")
        try:
            completed_dt = datetime.fromisoformat(raw)
        except ValueError:
            try:
                completed_dt = datetime.fromisoformat(raw.replace(" ", "T"))
            except ValueError:
                completed_dt = None
        if completed_dt is None:
            continue
        if completed_dt.tzinfo is None:
            completed_dt = completed_dt.replace(tzinfo=timezone.utc)
        if completed_dt < cutoff:
            continue
        haystack = " ".join(
            part for part in (project_id, row["client_name"], row["market"]) if part
        ).casefold()
        if needle and needle not in haystack:
            continue
        items.append(
            CompletedProjectListItemReadModel(
                project_id=project_id,
                client_name=row["client_name"],
                market=row["market"],
                project_status=str(row["project_status"]),
                ball_with=str(row["ball_with"]),
                current_milestone=row["current_milestone"],
                operator_id=str(row["operator_id"] or "default_operator"),
                completed_at=completed_at,
                days_since_completed=_age_days(completed_at) or 0,
            )
        )

    return items, _facets(
        {
            "market": ("Market", (item.market or "Unspecified" for item in items)),
            "milestone": ("Milestone", (item.current_milestone or "Not set" for item in items)),
        }
    )
