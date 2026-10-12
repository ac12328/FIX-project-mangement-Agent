"""CRUD operations for personal work item detail records."""

from __future__ import annotations

import hashlib
import sqlite3
from datetime import datetime, timezone

from src.models.personal_work_item import PersonalWorkItem
from src.repositories.work_item_repo import create_work_item


def create_personal_work_item(
    conn: sqlite3.Connection,
    *,
    kind: str,
    title: str,
    operator_id: str = "default_operator",
    description: str | None = None,
    priority: str = "medium",
    owner_type: str = "operator",
    owner_ref: str | None = None,
    related_entity_type: str | None = None,
    related_entity_id: str | None = None,
    due_at: str | None = None,
    remind_at: str | None = None,
    source_type: str = "manual",
    source_ref: str | None = None,
    evidence_text: str | None = None,
    confidence: float = 1.0,
    review_status: str | None = None,
    dedup_key: str | None = None,
    commit: bool = True,
) -> PersonalWorkItem:
    """Create a work item; pass commit=False inside a caller-owned write scope."""
    if kind not in {"task", "commitment", "waiting_for", "reminder"}:
        raise ValueError(f"Unsupported personal work item kind: {kind}")
    effective_review = review_status or (
        "proposed" if source_type in {"email", "meeting", "agent"} else "not_required"
    )
    effective_dedup = dedup_key or _dedup_key(
        operator_id, kind, source_type, source_ref, title
    )
    existing = conn.execute(
        """SELECT work_item_id FROM personal_work_items
           WHERE operator_id = ? AND dedup_key = ? AND review_status != 'rejected'""",
        (operator_id, effective_dedup),
    ).fetchone()
    if existing:
        return get_personal_work_item(conn, existing["work_item_id"])  # type: ignore[return-value]

    work_item = create_work_item(
        conn,
        "standalone",
        title,
        entity_table=None,
        entity_id=None,
        priority=priority,
    )
    conn.execute(
        """INSERT INTO personal_work_items (
               work_item_id, operator_id, kind, description, owner_type,
               owner_ref, related_entity_type, related_entity_id, due_at,
               remind_at, source_type, source_ref, evidence_text, confidence,
               review_status, dedup_key
           ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            work_item.work_item_id,
            operator_id,
            kind,
            description,
            owner_type,
            owner_ref,
            related_entity_type,
            related_entity_id,
            due_at,
            remind_at,
            source_type,
            source_ref,
            evidence_text,
            confidence,
            effective_review,
            effective_dedup,
        ),
    )
    if commit:
        conn.commit()
    return get_personal_work_item(conn, work_item.work_item_id)  # type: ignore[return-value]


def get_personal_work_item(
    conn: sqlite3.Connection, work_item_id: str
) -> PersonalWorkItem | None:
    row = conn.execute(
        """SELECT w.title, w.status, w.priority, p.*
           FROM work_items w
           JOIN personal_work_items p ON p.work_item_id = w.work_item_id
           WHERE w.work_item_id = ?""",
        (work_item_id,),
    ).fetchone()
    return PersonalWorkItem(**dict(row)) if row else None


def list_personal_work_items(
    conn: sqlite3.Connection,
    operator_id: str,
    *,
    kind: str | None = None,
    status: str | None = "open",
    review_status: str | None = None,
    limit: int = 100,
) -> list[PersonalWorkItem]:
    clauses = ["p.operator_id = ?"]
    params: list = [operator_id]
    if kind:
        clauses.append("p.kind = ?")
        params.append(kind)
    if status:
        clauses.append("w.status = ?")
        params.append(status)
    if review_status:
        clauses.append("p.review_status = ?")
        params.append(review_status)
    params.append(limit)
    rows = conn.execute(
        f"""SELECT w.title, w.status, w.priority, p.*
            FROM work_items w
            JOIN personal_work_items p ON p.work_item_id = w.work_item_id
            WHERE {' AND '.join(clauses)}
            ORDER BY COALESCE(p.due_at, p.remind_at, p.created_at), p.created_at
            LIMIT ?""",
        params,
    ).fetchall()
    return [PersonalWorkItem(**dict(row)) for row in rows]


def set_review_status(
    conn: sqlite3.Connection, work_item_id: str, review_status: str, *, commit: bool = True
) -> bool:
    cursor = conn.execute(
        """UPDATE personal_work_items SET review_status = ?, updated_at = datetime('now')
           WHERE work_item_id = ?""",
        (review_status, work_item_id),
    )
    if commit:
        conn.commit()
    return cursor.rowcount == 1


def resolve_personal_work_item(
    conn: sqlite3.Connection, work_item_id: str, resolution_note: str | None = None,
    *, commit: bool = True,
) -> bool:
    now = datetime.now(timezone.utc).isoformat()
    cursor = conn.execute(
        "UPDATE work_items SET status = 'resolved' WHERE work_item_id = ?",
        (work_item_id,),
    )
    conn.execute(
        """UPDATE personal_work_items
           SET completed_at = ?, resolution_note = ?, updated_at = ?
           WHERE work_item_id = ?""",
        (now, resolution_note, now, work_item_id),
    )
    if commit:
        conn.commit()
    return cursor.rowcount == 1


def cancel_personal_work_item(
    conn: sqlite3.Connection, work_item_id: str, *, commit: bool = True
) -> bool:
    cursor = conn.execute(
        "UPDATE work_items SET status = 'cancelled' WHERE work_item_id = ?",
        (work_item_id,),
    )
    if commit:
        conn.commit()
    return cursor.rowcount == 1


def snooze_personal_work_item(
    conn: sqlite3.Connection, work_item_id: str, snoozed_until: str,
    *, commit: bool = True,
) -> bool:
    _require_aware_timestamp(snoozed_until, "snoozed_until")
    cursor = conn.execute(
        """UPDATE personal_work_items
           SET snoozed_until = ?, updated_at = datetime('now')
           WHERE work_item_id = ?""",
        (snoozed_until, work_item_id),
    )
    if commit:
        conn.commit()
    return cursor.rowcount == 1


def _require_aware_timestamp(value: str, field_name: str) -> None:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError(f"{field_name} must be an ISO 8601 timestamp") from exc
    if parsed.tzinfo is None:
        raise ValueError(f"{field_name} must include a timezone offset")


def _dedup_key(
    operator_id: str,
    kind: str,
    source_type: str,
    source_ref: str | None,
    title: str,
) -> str:
    raw = "|".join(
        [operator_id, kind, source_type, source_ref or "", title.strip().lower()]
    )
    return hashlib.sha256(raw.encode()).hexdigest()
