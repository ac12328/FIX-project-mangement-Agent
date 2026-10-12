"""Caller-owned personal work-item state changes and linked reminder schedules."""

from __future__ import annotations

import sqlite3
from typing import Literal

from src.db.savepoint import savepoint
from src.models.personal_work_item import PersonalWorkItem
from src.repositories.personal_work_item_repo import (
    cancel_personal_work_item,
    get_personal_work_item,
    resolve_personal_work_item,
    set_review_status,
    snooze_personal_work_item,
)
from src.repositories.schedule_repo import (
    cancel_reminder_for_work_item,
    snooze_reminder_for_work_item,
)

WorkItemAction = Literal["complete", "cancel", "snooze", "confirm", "reject"]


def change_personal_work_item(
    conn: sqlite3.Connection,
    *,
    operator_id: str,
    work_item_id: str,
    action: WorkItemAction,
    snoozed_until: str | None = None,
    resolution_note: str | None = None,
) -> PersonalWorkItem | None:
    """Change an owned item and its reminder in one caller-owned command."""
    if action not in {"complete", "cancel", "snooze", "confirm", "reject"}:
        raise ValueError(f"Unsupported personal work item action: {action}")
    if action == "snooze" and not snoozed_until:
        raise ValueError("snoozed_until is required for snooze")

    with savepoint(conn, ensure_transaction=True):
        item = get_personal_work_item(conn, work_item_id)
        if item is None or item.operator_id != operator_id:
            return None

        if action == "complete":
            changed = resolve_personal_work_item(
                conn, work_item_id, resolution_note, commit=False
            )
        elif action in {"cancel", "reject"}:
            if action == "reject":
                set_review_status(conn, work_item_id, "rejected", commit=False)
            changed = cancel_personal_work_item(conn, work_item_id, commit=False)
        elif action == "snooze":
            changed = snooze_personal_work_item(
                conn, work_item_id, snoozed_until, commit=False
            )
        else:
            changed = set_review_status(conn, work_item_id, "confirmed", commit=False)

        if not changed:
            raise RuntimeError("Personal work item changed during the command")
        if item.kind == "reminder":
            if action in {"complete", "cancel", "reject"}:
                cancel_reminder_for_work_item(conn, work_item_id, commit=False)
            elif action == "snooze":
                snooze_reminder_for_work_item(
                    conn, work_item_id, snoozed_until, commit=False
                )
        return get_personal_work_item(conn, work_item_id)
