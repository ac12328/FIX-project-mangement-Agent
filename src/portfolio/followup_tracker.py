"""
Internal Follow-up Tracker (Blueprint Section 19 / Plan Step 3.4).
Tracks internal actions with auto-escalation.
Escalation: Level 0→1 (2 biz days) → auto-nudge; Level 1→2 (1 biz day) → Escalation Card.
SNOOZE max 3 times.
"""

import sqlite3
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone, timedelta


MAX_SNOOZE = 3
ESCALATION_L0_TO_L1_DAYS = 2  # Business days before auto-nudge
ESCALATION_L1_TO_L2_DAYS = 1  # Business days before escalation card
RECENT_TELEGRAM_ACTIVITY_MINUTES = 60  # Skip escalation if agent acted on this project recently


@dataclass
class FollowupAction:
    """Result of a follow-up check."""
    followup_id: str
    project_id: str
    action: str  # none | nudge | escalate | overdue
    description: str
    escalation_level: int
    days_overdue: int = 0


def create_followup(
    conn: sqlite3.Connection,
    project_id: str,
    description: str,
    due_date: str | None,
    *,
    owner_contact_id: str | None = None,
    linked_message_ref: str | None = None,
    created_by: str = "operator",
    reminder_due_at: str | None = None,
    owner_side: str | None = None,
    commit: bool = True,
) -> str:
    """Create a new internal follow-up. Returns followup_id."""
    project = conn.execute("SELECT project_status FROM projects WHERE project_id=?", (project_id,)).fetchone()
    if project is None:
        raise ValueError("project not found")
    if project[0] in {"Completed", "Cancelled", "Archived"}:
        raise ValueError("terminal onboarding project: track new work in a linked support ticket")
    if owner_side not in {None, "Internal", "External", "Shared"}:
        raise ValueError("invalid followup owner side")
    basis = "none" if due_date is None else "reported_commitment" if created_by == "auto_resolved" else "operator_entered"
    followup_id = f"fup_{uuid.uuid4().hex[:12]}"
    now = datetime.now(timezone.utc).isoformat()
    owns_transaction = not conn.in_transaction
    if owns_transaction:
        conn.execute("BEGIN")
    savepoint = "create_followup_" + uuid.uuid4().hex
    conn.execute("SAVEPOINT " + savepoint)
    try:
        conn.execute(
            """INSERT INTO internal_followups
               (followup_id, project_id, owner_contact_id, description,
                due_date, status, escalation_level, linked_message_ref,
                created_by, created_at, reminder_due_at, owner_side, due_date_basis)
               VALUES (?, ?, ?, ?, ?, 'open', 0, ?, ?, ?, ?, ?, ?)""",
            (followup_id, project_id, owner_contact_id, description,
             due_date, linked_message_ref, created_by, now, reminder_due_at, owner_side, basis),
        )
        from src.services.followup_plan import refresh_enrolled_followup_plan
        refresh_enrolled_followup_plan(conn, project_id, followup_id, "create")
    except Exception:
        conn.execute("ROLLBACK TO SAVEPOINT " + savepoint)
        conn.execute("RELEASE SAVEPOINT " + savepoint)
        if owns_transaction:
            conn.rollback()
        raise
    conn.execute("RELEASE SAVEPOINT " + savepoint)
    if commit:
        conn.commit()
    return followup_id


def get_followup(conn: sqlite3.Connection, followup_id: str) -> dict | None:
    """Get a follow-up by ID."""
    row = conn.execute(
        "SELECT * FROM internal_followups WHERE followup_id = ?", (followup_id,)
    ).fetchone()
    return dict(row) if row else None


def complete_followup(conn: sqlite3.Connection, followup_id: str,
                      resolution_note: str = "", *, expected_revision: int | None = None,
                      commit: bool = True) -> bool:
    """Complete open/overdue work; reject stale snapshots without partial writes."""
    fup = get_followup(conn, followup_id)
    if fup is None or fup["status"] not in {"open", "overdue"}:
        return False
    revision = fup["action_revision"]
    if expected_revision is not None and revision != expected_revision:
        return False
    changed = conn.execute(
        "UPDATE internal_followups SET status='completed',resolved_at=?,resolution_note=?,action_revision=action_revision+1 "
        "WHERE followup_id=? AND action_revision=? AND status IN ('open','overdue')",
        (datetime.now(timezone.utc).isoformat(), resolution_note, followup_id, revision),
    )
    if changed.rowcount == 1:
        from src.services.followup_plan import refresh_enrolled_followup_plan
        refresh_enrolled_followup_plan(conn, fup["project_id"], followup_id, "complete", before=fup)
    if commit:
        conn.commit()
    return changed.rowcount == 1


def snooze_followup(
    conn: sqlite3.Connection,
    followup_id: str,
    new_due_date: str,
    *, expected_revision: int | None = None, commit: bool = True,
) -> tuple[bool, str]:
    """Snooze a follow-up (max 3 times).
    Returns (success, reason).
    If max snoozes reached, escalates to Level 2 and returns (False, "max_snoozes").
    """
    fup = get_followup(conn, followup_id)
    if fup is None or fup["status"] not in {"open", "overdue"}:
        return False, "not_found"
    if expected_revision is not None and fup["action_revision"] != expected_revision:
        return False, "stale_followup"

    # Track snooze count in resolution_note (simple approach)
    snooze_count = _get_snooze_count(fup)
    if snooze_count >= MAX_SNOOZE:
        # Force-escalate to Level 2 per Blueprint Section 21.3
        _escalate_to_level2(conn, followup_id, "Max snoozes reached", commit=commit)
        return False, "max_snoozes"

    note = fup.get("resolution_note") or ""
    new_note = f"{note}|snooze:{datetime.now(timezone.utc).isoformat()}"

    changed = conn.execute(
        "UPDATE internal_followups SET reminder_due_at=?,resolution_note=?,action_revision=action_revision+1 "
        "WHERE followup_id=? AND action_revision=? AND status IN ('open','overdue')",
        (new_due_date, new_note, followup_id, fup["action_revision"]),
    )
    if changed.rowcount == 1:
        from src.services.followup_plan import refresh_enrolled_followup_plan
        refresh_enrolled_followup_plan(conn, fup["project_id"], followup_id, "snooze", before=fup)
    if commit:
        conn.commit()
    return (True, "ok") if changed.rowcount == 1 else (False, "stale_followup")


def _escalate_to_level2(conn: sqlite3.Connection, followup_id: str, reason: str, *, commit: bool = True) -> None:
    """Force-escalate a follow-up to Level 2."""
    now = datetime.now(timezone.utc).isoformat()
    conn.execute(
        """UPDATE internal_followups
           SET action_revision=action_revision+1, escalation_level = 2, reminder_sent_at = ?, resolution_note = COALESCE(resolution_note, '') || ? || '|escalate_l2:'
           WHERE followup_id = ?""",
        (now, reason, followup_id),
    )
    if commit:
        conn.commit()


def cancel_followup(conn: sqlite3.Connection, followup_id: str, *,
                    expected_revision: int | None = None, commit: bool = True) -> bool:
    fup = get_followup(conn, followup_id)
    if fup is None or fup["status"] not in {"open", "overdue"}:
        return False
    revision = fup["action_revision"]
    if expected_revision is not None and expected_revision != revision:
        return False
    changed = conn.execute("UPDATE internal_followups SET status='cancelled',action_revision=action_revision+1 "
        "WHERE followup_id=? AND action_revision=? AND status IN ('open','overdue')", (followup_id, revision))
    if changed.rowcount == 1:
        from src.services.followup_plan import refresh_enrolled_followup_plan
        refresh_enrolled_followup_plan(conn, fup["project_id"], followup_id, "cancel", before=fup)
    if commit:
        conn.commit()
    return changed.rowcount == 1


def reassign_followup(conn: sqlite3.Connection, followup_id: str, contact_id: str, *,
                      expected_revision: int | None = None, commit: bool = True) -> bool:
    """Reassign existing open work; do not report success for missing IDs."""
    fup = get_followup(conn, followup_id)
    contact = conn.execute("SELECT c.contact_type,o.org_type FROM contacts c LEFT JOIN organisations o ON o.org_id=c.organisation_id WHERE c.contact_id=?", (contact_id,)).fetchone()
    if not fup or fup["status"] not in {"open", "overdue"} or not contact:
        return False
    revision = fup["action_revision"]
    if expected_revision is not None and expected_revision != revision:
        return False
    side = "Internal" if contact["contact_type"] == "internal" or contact["org_type"] == "internal" else "External" if contact["org_type"] in {"client", "vendor", "regulator"} else None
    changed = conn.execute("UPDATE internal_followups SET owner_contact_id=?,owner_side=?,action_revision=action_revision+1 "
        "WHERE followup_id=? AND action_revision=? AND status IN ('open','overdue')", (contact_id, side, followup_id, revision))
    if changed.rowcount == 1:
        from src.services.followup_plan import refresh_enrolled_followup_plan
        refresh_enrolled_followup_plan(conn, fup["project_id"], followup_id, "reassign", before=fup)
    if commit:
        conn.commit()
    return changed.rowcount == 1


def _had_recent_telegram_activity(conn: sqlite3.Connection, project_id: str, now: datetime) -> bool:
    """Return True if there was a Telegram surface activity_log entry in the last RECENT_TELEGRAM_ACTIVITY_MINUTES."""
    cutoff = (now - timedelta(minutes=RECENT_TELEGRAM_ACTIVITY_MINUTES)).isoformat()
    row = conn.execute(
        """SELECT 1 FROM activity_log
           WHERE project_id = ? AND surface = 'telegram' AND created_at >= ?
           LIMIT 1""",
        (project_id, cutoff),
    ).fetchone()
    return row is not None


def check_escalations(conn: sqlite3.Connection) -> list[FollowupAction]:
    """
    Check all open follow-ups for escalation.
    Returns list of actions to take.
    """
    now = datetime.now(timezone.utc)
    rows = conn.execute(
        """SELECT * FROM internal_followups
           WHERE status IN ('open','overdue') AND (due_date IS NOT NULL OR reminder_due_at IS NOT NULL)""",
    ).fetchall()

    actions = []
    for row in rows:
        fup = dict(row)
        if _had_recent_telegram_activity(conn, fup["project_id"], now):
            continue
        action = _check_single_escalation(conn, fup, now)
        if action.action != "none":
            actions.append(action)

    return actions


def resolve_open_followups_for_terminal_project(
    conn: sqlite3.Connection,
    project_id: str,
    terminal_status: str,
) -> int:
    """Resolve open follow-ups when a project enters a terminal status.

    Completed/Archived → completed; Cancelled → cancelled.
    Does not commit — caller owns the transaction.
    """
    from src.models.enums import ProjectStatus

    if terminal_status not in {s.value for s in ProjectStatus.terminal_statuses()}:
        return 0

    open_count_row = conn.execute(
        "SELECT COUNT(*) FROM internal_followups WHERE project_id = ? AND status IN ('open','overdue')",
        (project_id,),
    ).fetchone()
    if not open_count_row or open_count_row[0] == 0:
        return 0

    if terminal_status == ProjectStatus.COMPLETED.value:
        # Completion is not evidence that residual technical work was resolved.
        # The verified PCT path transfers each item to a linked support ticket
        # before closing; generic closing guards reject this state when items remain.
        return 0

    if terminal_status == ProjectStatus.CANCELLED.value:
        conn.execute(
            "UPDATE internal_followups SET status = 'cancelled', action_revision=action_revision+1 WHERE project_id = ? AND status IN ('open','overdue')",
            (project_id,),
        )
    else:
        now = datetime.now(timezone.utc).isoformat()
        note = f"Auto-closed: project {terminal_status}"
        conn.execute(
            """UPDATE internal_followups
               SET status = 'completed', action_revision=action_revision+1, resolved_at = ?, resolution_note = ?
               WHERE project_id = ? AND status IN ('open','overdue')""",
            (now, note, project_id),
        )
    return int(open_count_row[0])


def get_open_followups(
    conn: sqlite3.Connection, project_id: str | None = None
) -> list[dict]:
    """Get all open follow-ups, optionally filtered by project."""
    if project_id:
        rows = conn.execute(
            """SELECT * FROM internal_followups
               WHERE project_id = ? AND status IN ('open','overdue')
               ORDER BY due_date""",
            (project_id,),
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT * FROM internal_followups WHERE status IN ('open','overdue') ORDER BY due_date"
        ).fetchall()
    return [dict(r) for r in rows]


def _check_single_escalation(
    conn: sqlite3.Connection, fup: dict, now: datetime
) -> FollowupAction:
    """Check escalation level for a single follow-up."""
    due = _parse_date(fup["due_date"])
    reminder = _parse_date(fup.get("reminder_due_at"))
    if reminder is not None:
        if reminder > now:
            return FollowupAction(fup["followup_id"], fup["project_id"], "none", fup["description"], fup["escalation_level"])
        changed = conn.execute(
            "UPDATE internal_followups SET reminder_due_at=NULL,reminder_sent_at=?,action_revision=action_revision+1 "
            "WHERE followup_id=? AND reminder_due_at=? AND status IN ('open','overdue')",
            (now.isoformat(), fup["followup_id"], fup["reminder_due_at"]),
        )
        return FollowupAction(fup["followup_id"], fup["project_id"], "nudge" if changed.rowcount else "none",
            fup["description"], fup["escalation_level"], max(0, (now-due).days) if due else 0)
    if due is None:
        return FollowupAction(
            followup_id=fup["followup_id"],
            project_id=fup["project_id"],
            action="none",
            description=fup["description"],
            escalation_level=fup["escalation_level"],
        )

    days_overdue = (now - due).days
    current_level = fup["escalation_level"]

    if days_overdue < 0:
        # Not yet due
        return FollowupAction(
            followup_id=fup["followup_id"],
            project_id=fup["project_id"],
            action="none",
            description=fup["description"],
            escalation_level=current_level,
        )

    action = "overdue"
    if current_level == 0 and days_overdue >= ESCALATION_L0_TO_L1_DAYS:
        # Escalate to level 1 — send nudge
        changed = conn.execute(
            "UPDATE internal_followups SET action_revision=action_revision+1, escalation_level = 1, reminder_sent_at = ? "
            "WHERE followup_id = ? AND action_revision = ? AND status IN ('open','overdue')",
            (now.isoformat(), fup["followup_id"], fup["action_revision"]),
        )
        action = "nudge" if changed.rowcount else "none"
    elif current_level == 1 and days_overdue >= ESCALATION_L0_TO_L1_DAYS + ESCALATION_L1_TO_L2_DAYS:
        # Escalate to level 2 — operator escalation card
        changed = conn.execute(
            "UPDATE internal_followups SET action_revision=action_revision+1, escalation_level = 2 "
            "WHERE followup_id = ? AND action_revision = ? AND status IN ('open','overdue')",
            (fup["followup_id"], fup["action_revision"]),
        )
        action = "escalate" if changed.rowcount else "none"

    return FollowupAction(
        followup_id=fup["followup_id"],
        project_id=fup["project_id"],
        action=action,
        description=fup["description"],
        escalation_level=max(current_level, 1 if action == "nudge" else (2 if action == "escalate" else current_level)),
        days_overdue=days_overdue,
    )


def _get_snooze_count(fup: dict) -> int:
    """Count number of snoozes from resolution_note."""
    note = fup.get("resolution_note") or ""
    return note.count("|snooze:")


def _parse_date(date_str: str | None) -> datetime | None:
    if not date_str:
        return None
    try:
        dt = datetime.fromisoformat(date_str)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except (ValueError, TypeError):
        return None
