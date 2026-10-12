"""One-shot reminder creation and missed-run policy."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from src.db.savepoint import savepoint
from src.repositories.personal_work_item_repo import create_personal_work_item
from src.repositories.schedule_repo import create_one_shot_schedule


def create_one_shot_reminder(
    conn,
    *,
    operator_id: str,
    title: str,
    remind_at: str,
    timezone_name: str = "Asia/Hong_Kong",
    related_entity_type: str | None = None,
    related_entity_id: str | None = None,
    source_type: str = "manual",
) -> dict:
    run_at = _require_aware_future(remind_at)
    with savepoint(conn, ensure_transaction=True):
        item = create_personal_work_item(
            conn,
            kind="reminder",
            title=title,
            operator_id=operator_id,
            remind_at=run_at,
            related_entity_type=related_entity_type,
            related_entity_id=related_entity_id,
            source_type=source_type,
            commit=False,
        )
        schedule = create_one_shot_schedule(
            conn,
            operator_id=operator_id,
            title=title,
            run_at=run_at,
            timezone_name=timezone_name,
            command_payload={"work_item_id": item.work_item_id, "title": title},
            dedup_key=f"reminder:{item.work_item_id}",
            commit=False,
        )
        return {"work_item": item.model_dump(), "schedule": schedule}


def is_missed_beyond_catchup(scheduled_for: str, *, now: datetime | None = None) -> bool:
    current = now or datetime.now(timezone.utc)
    scheduled = datetime.fromisoformat(scheduled_for.replace("Z", "+00:00"))
    return current - scheduled > timedelta(hours=24)


def _require_aware_future(value: str) -> str:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError("remind_at must be an ISO 8601 timestamp") from exc
    if parsed.tzinfo is None:
        raise ValueError("remind_at must include a timezone offset")
    return parsed.astimezone(timezone.utc).isoformat()
