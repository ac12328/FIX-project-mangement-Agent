"""Persistence for immutable meeting-note ingestion records."""

from __future__ import annotations

import json
import sqlite3

from src.models.meeting_ingestion import MeetingIngestion, MeetingExtractionItem


def insert_meeting_ingestion(
    conn: sqlite3.Connection,
    ingestion: MeetingIngestion,
    *,
    commit: bool = True,
) -> MeetingIngestion:
    conn.execute(
        """INSERT INTO meeting_ingestions (
               ingestion_id, operator_id, calendar_event_id, source_hash,
               source_text, extracted_json, work_item_ids_json, created_at
           ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            ingestion.ingestion_id,
            ingestion.operator_id,
            ingestion.calendar_event_id,
            ingestion.source_hash,
            ingestion.source_text,
            json.dumps(
                [item.model_dump() for item in ingestion.extracted_items],
                ensure_ascii=False,
            ),
            json.dumps(ingestion.work_item_ids),
            ingestion.created_at,
        ),
    )
    if commit:
        conn.commit()
    return ingestion


def get_meeting_ingestion(
    conn: sqlite3.Connection,
    ingestion_id: str,
    operator_id: str,
) -> MeetingIngestion | None:
    row = conn.execute(
        """SELECT * FROM meeting_ingestions
           WHERE ingestion_id = ? AND operator_id = ?""",
        (ingestion_id, operator_id),
    ).fetchone()
    return _row(row) if row else None


def get_ingestion_by_source(
    conn: sqlite3.Connection,
    *,
    operator_id: str,
    calendar_event_id: str,
    source_hash: str,
) -> MeetingIngestion | None:
    row = conn.execute(
        """SELECT * FROM meeting_ingestions
           WHERE operator_id = ? AND calendar_event_id = ? AND source_hash = ?""",
        (operator_id, calendar_event_id, source_hash),
    ).fetchone()
    return _row(row) if row else None


def list_meeting_ingestions(
    conn: sqlite3.Connection,
    *,
    operator_id: str,
    calendar_event_id: str,
    limit: int = 20,
) -> list[MeetingIngestion]:
    rows = conn.execute(
        """SELECT * FROM meeting_ingestions
           WHERE operator_id = ? AND calendar_event_id = ?
           ORDER BY created_at DESC LIMIT ?""",
        (operator_id, calendar_event_id, max(1, min(limit, 100))),
    ).fetchall()
    return [_row(row) for row in rows]


def _row(row: sqlite3.Row) -> MeetingIngestion:
    return MeetingIngestion(
        ingestion_id=row["ingestion_id"],
        operator_id=row["operator_id"],
        calendar_event_id=row["calendar_event_id"],
        source_hash=row["source_hash"],
        source_text=row["source_text"],
        extracted_items=[
            MeetingExtractionItem(**item)
            for item in json.loads(row["extracted_json"] or "[]")
        ],
        work_item_ids=json.loads(row["work_item_ids_json"] or "[]"),
        created_at=row["created_at"],
    )
