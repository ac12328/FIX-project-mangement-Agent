"""Persistence and transactional claiming for personal assistant schedules."""

from __future__ import annotations

import hashlib
import json
import sqlite3
import uuid
from datetime import datetime, timedelta, timezone


def create_one_shot_schedule(
    conn: sqlite3.Connection,
    *,
    operator_id: str,
    title: str,
    run_at: str,
    timezone_name: str = "Asia/Hong_Kong",
    command_type: str = "reminder_notify",
    command_payload: dict | None = None,
    channel: str = "telegram",
    urgency: str = "normal",
    dedup_key: str | None = None,
    commit: bool = True,
) -> dict:
    schedule_id = f"sch_{uuid.uuid4().hex[:12]}"
    effective_dedup = dedup_key or _dedup(operator_id, title, run_at, command_type)
    try:
        conn.execute(
            """INSERT INTO assistant_schedules (
                   schedule_id, operator_id, title, trigger_type, trigger_value,
                   timezone, command_type, command_payload_json, channel,
                   urgency, dedup_key, next_run_at
               ) VALUES (?, ?, ?, 'at', ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                schedule_id,
                operator_id,
                title,
                run_at,
                timezone_name,
                command_type,
                json.dumps(command_payload or {}, ensure_ascii=False),
                channel,
                urgency,
                effective_dedup,
                run_at,
            ),
        )
    except sqlite3.IntegrityError:
        row = conn.execute(
            """SELECT * FROM assistant_schedules
               WHERE operator_id = ? AND dedup_key = ? AND enabled = 1""",
            (operator_id, effective_dedup),
        ).fetchone()
        if row:
            return _row(row)
        raise
    if commit:
        conn.commit()
    return get_schedule(conn, schedule_id)  # type: ignore[return-value]


def get_schedule(conn: sqlite3.Connection, schedule_id: str) -> dict | None:
    row = conn.execute(
        "SELECT * FROM assistant_schedules WHERE schedule_id = ?", (schedule_id,)
    ).fetchone()
    return _row(row) if row else None


def claim_due_schedules(
    conn: sqlite3.Connection,
    *,
    now: str,
    limit: int = 20,
    commit: bool = True,
) -> list[dict]:
    rows = conn.execute(
        """SELECT * FROM assistant_schedules
           WHERE enabled = 1 AND next_run_at IS NOT NULL AND next_run_at <= ?
           ORDER BY next_run_at LIMIT ?""",
        (now, limit),
    ).fetchall()
    claimed: list[dict] = []
    for row in rows:
        schedule = _row(row)
        run_id = f"run_{uuid.uuid4().hex[:12]}"
        try:
            conn.execute(
                """INSERT INTO assistant_schedule_runs
                   (run_id, schedule_id, scheduled_for, intent_version, status)
                   VALUES (?, ?, ?, ?, 'claimed')""",
                (run_id, schedule["schedule_id"], schedule["next_run_at"],
                 schedule["intent_version"]),
            )
        except sqlite3.IntegrityError:
            continue
        # Hide the due row while delivery is in progress so a failed run can be
        # explicitly re-queued rather than being reclaimed concurrently.
        conn.execute(
            """UPDATE assistant_schedules
               SET next_run_at = NULL, updated_at = datetime('now')
               WHERE schedule_id = ?""",
            (schedule["schedule_id"],),
        )
        schedule["run_id"] = run_id
        claimed.append(schedule)
    if commit:
        conn.commit()
    return claimed


def complete_run(
    conn: sqlite3.Connection,
    run_id: str,
    schedule_id: str,
    *,
    status: str,
    result: dict | None = None,
    error: str | None = None,
    commit: bool = True,
) -> bool:
    """Complete a run without overwriting a later user edit to its schedule.

    Return whether this occurrence still owned and finalized the schedule.
    """
    now = datetime.now(timezone.utc).isoformat()
    conn.execute(
        """UPDATE assistant_schedule_runs
           SET status = ?, result_json = ?, error = ?, finished_at = ?
           WHERE run_id = ?""",
        (status, json.dumps(result or {}, ensure_ascii=False), error, now, run_id),
    )
    schedule_updated = False
    if status in {"sent", "completed", "missed"}:
        cursor = conn.execute(
            """UPDATE assistant_schedules
               SET enabled = 0, next_run_at = NULL, last_run_at = ?, updated_at = ?
               WHERE schedule_id = ? AND enabled = 1 AND next_run_at IS NULL
                 AND intent_version = (
                     SELECT intent_version FROM assistant_schedule_runs
                     WHERE run_id = ? AND schedule_id = ?
                 )""",
            (now, now, schedule_id, run_id, schedule_id),
        )
        schedule_updated = cursor.rowcount == 1
    elif status == "failed":
        # Retry on the next scheduler tick. The run-history uniqueness key still
        # prevents duplicate claims for the failed scheduled occurrence, so move
        # the occurrence forward by five minutes.
        retry_at = (
            datetime.now(timezone.utc).replace(microsecond=0)
            + timedelta(minutes=5)
        ).isoformat()
        cursor = conn.execute(
            """UPDATE assistant_schedules
               SET next_run_at = ?, updated_at = ?
               WHERE schedule_id = ? AND enabled = 1 AND next_run_at IS NULL
                 AND intent_version = (
                     SELECT intent_version FROM assistant_schedule_runs
                     WHERE run_id = ? AND schedule_id = ?
                 )""",
            (retry_at, now, schedule_id, run_id, schedule_id),
        )
        schedule_updated = cursor.rowcount == 1
    if commit:
        conn.commit()
    return schedule_updated


def snooze_schedule(conn: sqlite3.Connection, schedule_id: str, next_run_at: str) -> bool:
    cursor = conn.execute(
        """UPDATE assistant_schedules
           SET enabled = 1, next_run_at = ?, trigger_value = ?,
               intent_version = intent_version + 1, updated_at = datetime('now')
           WHERE schedule_id = ?""",
        (next_run_at, next_run_at, schedule_id),
    )
    conn.commit()
    return cursor.rowcount == 1


def cancel_schedule(conn: sqlite3.Connection, schedule_id: str) -> bool:
    cursor = conn.execute(
        """UPDATE assistant_schedules
           SET enabled = 0, next_run_at = NULL,
               intent_version = intent_version + 1, updated_at = datetime('now')
           WHERE schedule_id = ?""",
        (schedule_id,),
    )
    conn.commit()
    return cursor.rowcount == 1


def snooze_reminder_for_work_item(
    conn: sqlite3.Connection, work_item_id: str, next_run_at: str,
    *, commit: bool = True,
) -> bool:
    """Move the active reminder schedule linked to a personal work item."""
    cursor = conn.execute(
        """UPDATE assistant_schedules
           SET enabled = 1, next_run_at = ?, trigger_value = ?,
               intent_version = intent_version + 1, updated_at = datetime('now')
           WHERE command_type = 'reminder_notify'
             AND json_extract(command_payload_json, '$.work_item_id') = ?""",
        (next_run_at, next_run_at, work_item_id),
    )
    if commit:
        conn.commit()
    return cursor.rowcount > 0


def cancel_reminder_for_work_item(
    conn: sqlite3.Connection, work_item_id: str, *, commit: bool = True
) -> bool:
    """Disable the active reminder schedule linked to a personal work item."""
    cursor = conn.execute(
        """UPDATE assistant_schedules
           SET enabled = 0, next_run_at = NULL,
               intent_version = intent_version + 1, updated_at = datetime('now')
           WHERE command_type = 'reminder_notify'
             AND json_extract(command_payload_json, '$.work_item_id') = ?""",
        (work_item_id,),
    )
    if commit:
        conn.commit()
    return cursor.rowcount > 0


def _row(row: sqlite3.Row) -> dict:
    data = dict(row)
    data["command_payload"] = json.loads(data.pop("command_payload_json") or "{}")
    data["enabled"] = bool(data["enabled"])
    return data


def _dedup(operator_id: str, title: str, run_at: str, command_type: str) -> str:
    return hashlib.sha256(
        f"{operator_id}|{title.strip().lower()}|{run_at}|{command_type}".encode()
    ).hexdigest()
