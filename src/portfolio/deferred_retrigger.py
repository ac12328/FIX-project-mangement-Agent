"""
Deferred ProposedUpdate Re-trigger (Blueprint Section 20 / Plan Step 3.12).
3 re_trigger_condition types: on_event, on_status_change, on_date.
Max 5 re-triggers then escalate.
"""

import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone


MAX_RETRIGGERS = 5


@dataclass
class DeferredCheck:
    """Result of checking a deferred item."""
    deferred_id: int
    proposed_update_id: str
    should_trigger: bool
    trigger_reason: str = ""
    should_escalate: bool = False


def get_pending_deferred(conn: sqlite3.Connection) -> list[dict]:
    """Get all waiting deferred items."""
    rows = conn.execute(
        """SELECT * FROM deferred_queue
           WHERE status = 'waiting'
           ORDER BY deferred_at""",
    ).fetchall()
    return [dict(r) for r in rows]


def check_deferred_triggers(conn: sqlite3.Connection) -> list[DeferredCheck]:
    """
    Check all waiting deferred items for trigger conditions.
    Returns list of items that should be re-triggered or escalated.
    """
    now = datetime.now(timezone.utc)
    pending = get_pending_deferred(conn)
    results = []

    for item in pending:
        check = _evaluate_trigger(conn, item, now)
        if check.should_trigger or check.should_escalate:
            results.append(check)

    return results


def create_deferred(
    conn: sqlite3.Connection,
    proposed_update_id: str,
    trigger_condition: str,
    *,
    next_check_at: str | None = None,
) -> int:
    """Create a new deferred item. Returns the row id."""
    now = datetime.now(timezone.utc).isoformat()
    cursor = conn.execute(
        """INSERT INTO deferred_queue
           (proposed_update_id, re_trigger_condition, deferred_at, next_check_at, status)
           VALUES (?, ?, ?, ?, 'waiting')""",
        (proposed_update_id, trigger_condition, now, next_check_at),
    )
    conn.commit()
    return cursor.lastrowid


def mark_triggered(conn: sqlite3.Connection, deferred_id: int) -> None:
    """Mark a deferred item as triggered."""
    now = datetime.now(timezone.utc).isoformat()
    conn.execute(
        "UPDATE deferred_queue SET status = 'triggered', triggered_at = ? WHERE id = ?",
        (now, deferred_id),
    )
    conn.commit()


def dismiss_deferred(conn: sqlite3.Connection, deferred_id: int) -> None:
    """Dismiss a deferred item."""
    conn.execute(
        "UPDATE deferred_queue SET status = 'dismissed' WHERE id = ?",
        (deferred_id,),
    )
    conn.commit()


def _evaluate_trigger(
    conn: sqlite3.Connection, item: dict, now: datetime
) -> DeferredCheck:
    """Evaluate whether a deferred item's trigger condition is met."""
    condition = item.get("re_trigger_condition", "")

    # Check if next_check_at date has passed (simplest trigger type)
    next_check = item.get("next_check_at")
    should_trigger = False
    reason = ""

    if next_check:
        check_dt = _parse_date(next_check)
        if check_dt and now >= check_dt:
            should_trigger = True
            reason = f"Scheduled check date reached: {next_check}"

    if not should_trigger and condition.startswith("on_date:"):
        trigger_date = _parse_date(condition[len("on_date:"):])
        if trigger_date and now >= trigger_date:
            should_trigger = True
            reason = f"Date trigger: {condition}"

    # on_event:<event_type> — check activity_log for matching action_type
    if not should_trigger and condition.startswith("on_event:"):
        event_type = condition[len("on_event:"):]
        deferred_at = item.get("deferred_at", "")
        count = conn.execute(
            "SELECT COUNT(*) FROM activity_log WHERE action_type = ? AND created_at > ?",
            (event_type, deferred_at),
        ).fetchone()[0]
        if count > 0:
            should_trigger = True
            reason = f"Event trigger: {event_type} occurred ({count} times since deferral)"

    # on_status_change:<status> — check if project's current status matches
    if not should_trigger and condition.startswith("on_status_change:"):
        target_status = condition[len("on_status_change:"):]
        row = conn.execute(
            "SELECT project_id FROM proposed_updates WHERE update_id = ?",
            (item["proposed_update_id"],),
        ).fetchone()
        if row:
            proj = conn.execute(
                "SELECT project_status FROM projects WHERE project_id = ?",
                (row[0],),
            ).fetchone()
            if proj and proj[0] == target_status:
                should_trigger = True
                reason = f"Status change trigger: project reached {target_status}"

    # Default 30-day re-trigger
    if not should_trigger:
        deferred_at_dt = _parse_date(item.get("deferred_at"))
        if deferred_at_dt:
            days_deferred = (now - deferred_at_dt).days
            if days_deferred >= 30:
                should_trigger = True
                reason = f"Default 30-day re-trigger ({days_deferred} days deferred)"

    # Enforce MAX_RETRIGGERS — escalate if too many triggers
    should_escalate = False
    trigger_count = conn.execute(
        """SELECT COUNT(*) FROM deferred_queue
           WHERE proposed_update_id = ? AND status = 'triggered'""",
        (item["proposed_update_id"],),
    ).fetchone()[0]
    if trigger_count >= MAX_RETRIGGERS:
        should_escalate = True

    return DeferredCheck(
        deferred_id=item["id"],
        proposed_update_id=item["proposed_update_id"],
        should_trigger=should_trigger,
        trigger_reason=reason,
        should_escalate=should_escalate,
    )


def _parse_date(date_str: str | None) -> datetime | None:
    if not date_str:
        return None
    try:
        dt = datetime.fromisoformat(date_str.strip())
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except (ValueError, TypeError):
        return None
