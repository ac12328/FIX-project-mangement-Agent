"""Persistence for shared meeting preparation snapshots."""

from __future__ import annotations

import json
import sqlite3

from src.models.meeting_prep import MeetingPrepSnapshot


def save_meeting_prep(
    conn: sqlite3.Connection,
    snapshot: MeetingPrepSnapshot,
) -> MeetingPrepSnapshot:
    payload = snapshot.model_dump(exclude={
        "prep_id",
        "operator_id",
        "calendar_event_id",
        "event_raw_hash",
        "project_id",
        "generated_at",
    })
    conn.execute(
        """INSERT INTO meeting_prep_snapshots (
               prep_id, operator_id, calendar_event_id, event_raw_hash,
               project_id, payload_json, generated_at
           ) VALUES (?, ?, ?, ?, ?, ?, ?)
           ON CONFLICT(operator_id, calendar_event_id, event_raw_hash)
           DO UPDATE SET
               project_id = excluded.project_id,
               payload_json = excluded.payload_json,
               generated_at = excluded.generated_at""",
        (
            snapshot.prep_id,
            snapshot.operator_id,
            snapshot.calendar_event_id,
            snapshot.event_raw_hash,
            snapshot.project_id,
            json.dumps(payload, ensure_ascii=False),
            snapshot.generated_at,
        ),
    )
    conn.commit()
    return get_meeting_prep(
        conn,
        snapshot.calendar_event_id,
        snapshot.operator_id,
        event_raw_hash=snapshot.event_raw_hash,
    ) or snapshot


def get_meeting_prep(
    conn: sqlite3.Connection,
    calendar_event_id: str,
    operator_id: str,
    *,
    event_raw_hash: str | None = None,
) -> MeetingPrepSnapshot | None:
    clauses = ["calendar_event_id = ?", "operator_id = ?"]
    params: list = [calendar_event_id, operator_id]
    if event_raw_hash:
        clauses.append("event_raw_hash = ?")
        params.append(event_raw_hash)
    row = conn.execute(
        f"""SELECT * FROM meeting_prep_snapshots
            WHERE {' AND '.join(clauses)}
            ORDER BY generated_at DESC LIMIT 1""",
        params,
    ).fetchone()
    return _row(row) if row else None


def _row(row: sqlite3.Row) -> MeetingPrepSnapshot:
    data = json.loads(row["payload_json"] or "{}")
    data.update({
        "prep_id": row["prep_id"],
        "operator_id": row["operator_id"],
        "calendar_event_id": row["calendar_event_id"],
        "event_raw_hash": row["event_raw_hash"],
        "project_id": row["project_id"],
        "generated_at": row["generated_at"],
    })
    return MeetingPrepSnapshot(**data)
