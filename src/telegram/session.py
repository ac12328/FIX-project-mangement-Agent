"""
Telegram Session management (Plan Step 3.13b, Blueprint Section 35).
Configurable timeout (default 10 min), auto-cancel incomplete flows.
"""

import json
import sqlite3
import uuid
from datetime import datetime, timezone, timedelta


SESSION_TIMEOUT_MINUTES = 10  # default fallback


def _get_timeout_minutes(conn: sqlite3.Connection) -> int:
    """Read session_timeout_minutes from operator_state, falling back to default."""
    try:
        row = conn.execute(
            "SELECT session_timeout_minutes FROM operator_state WHERE id = 1"
        ).fetchone()
        if row and row[0] is not None:
            return max(1, int(row[0]))
    except Exception:
        pass  # column may not exist yet
    return SESSION_TIMEOUT_MINUTES


def create_session(
    conn: sqlite3.Connection,
    flow_type: str,
    *,
    originating_ref: str | None = None,
    operator_id: str = "default_operator",
    commit: bool = True,
) -> str:
    """Create a session; a command owner may defer commit with ``commit=False``."""
    session_id = f"tgs_{uuid.uuid4().hex[:12]}"
    now = datetime.now(timezone.utc)
    timeout_at = (now + timedelta(minutes=_get_timeout_minutes(conn))).isoformat()
    # Enforce one session per operator
    conn.execute(
        "UPDATE telegram_sessions SET status = 'cancelled' WHERE operator_id = ? AND status = 'active'",
        (operator_id,),
    )
    conn.execute(
        """INSERT INTO telegram_sessions
           (session_id, flow_type, originating_ref, operator_id,
            started_at, last_action_at, timeout_at, status)
           VALUES (?, ?, ?, ?, ?, ?, ?, 'active')""",
        (session_id, flow_type, originating_ref, operator_id,
         now.isoformat(), now.isoformat(), timeout_at),
    )
    if commit:
        conn.commit()
    return session_id


def get_session(conn: sqlite3.Connection, session_id: str) -> dict | None:
    """Get a session by ID."""
    row = conn.execute(
        "SELECT * FROM telegram_sessions WHERE session_id = ?",
        (session_id,),
    ).fetchone()
    return dict(row) if row else None


def get_active_session(
    conn: sqlite3.Connection,
    operator_id: str = "default_operator",
) -> dict | None:
    """Get the current active session for one operator, if any."""
    row = conn.execute(
        """SELECT * FROM telegram_sessions
           WHERE operator_id = ? AND status = 'active'
           ORDER BY started_at DESC
           LIMIT 1""",
        (operator_id,),
    ).fetchone()
    return dict(row) if row else None


def get_operator_input_session(
    conn: sqlite3.Connection,
    operator_id: str = "default_operator",
) -> dict | None:
    """Return the session that should receive the operator's next free-text reply."""
    session = get_active_session(conn, operator_id)
    if session:
        return session
    # resolution_learning may be timed_out while waiting for follow-up job or operator reply
    row = conn.execute(
        """SELECT * FROM telegram_sessions
           WHERE operator_id = ?
             AND flow_type = 'resolution_learning'
             AND status = 'timed_out'
           ORDER BY started_at DESC
           LIMIT 1""",
        (operator_id,),
    ).fetchone()
    return dict(row) if row else None


def _resolution_reply_timeout_minutes() -> int:
    from config.settings import CONFIG
    return max(30, int(getattr(CONFIG.thresholds, "resolution_reply_timeout_minutes", 1440)))


def touch_session(conn: sqlite3.Connection, session_id: str) -> bool:
    """Update last_action_at and extend timeout. Returns True if session accepts input."""
    session = get_session(conn, session_id)
    if not session:
        return False
    if session["status"] not in ("active", "timed_out"):
        return False
    now = datetime.now(timezone.utc)
    if session["flow_type"] == "resolution_learning":
        timeout_at = (now + timedelta(minutes=_resolution_reply_timeout_minutes())).isoformat()
    else:
        timeout_at = (now + timedelta(minutes=_get_timeout_minutes(conn))).isoformat()
    conn.execute(
        """UPDATE telegram_sessions
           SET last_action_at = ?, timeout_at = ?,
               status = CASE WHEN status = 'timed_out' THEN 'active' ELSE status END
           WHERE session_id = ?""",
        (now.isoformat(), timeout_at, session_id),
    )
    conn.commit()
    return True


def set_session_data(
    conn: sqlite3.Connection,
    session_id: str,
    intermediate_data: dict | None,
    *,
    commit: bool = True,
) -> bool:
    """Persist session data, optionally retaining the caller's transaction."""
    session = get_session(conn, session_id)
    if not session or session["status"] != "active":
        return False
    conn.execute(
        "UPDATE telegram_sessions SET intermediate_data = ? WHERE session_id = ?",
        (json.dumps(intermediate_data, ensure_ascii=False) if intermediate_data is not None else None, session_id),
    )
    if commit:
        conn.commit()
    return True


def complete_session(
    conn: sqlite3.Connection, session_id: str, *, commit: bool = True,
) -> bool:
    """Mark a session as completed; let a command owner defer the commit."""
    cur = conn.execute(
        """UPDATE telegram_sessions SET status = 'completed'
           WHERE session_id = ? AND status IN ('active', 'timed_out')""",
        (session_id,),
    )
    if commit:
        conn.commit()
    return cur.rowcount > 0


def cancel_session(conn: sqlite3.Connection, session_id: str) -> bool:
    """Cancel a session."""
    cur = conn.execute(
        "UPDATE telegram_sessions SET status = 'cancelled' WHERE session_id = ? AND status = 'active'",
        (session_id,),
    )
    conn.commit()
    return cur.rowcount > 0


def enforce_timeouts(conn: sqlite3.Connection) -> list[str]:
    """
    Find and time out all expired active sessions.
    Returns list of timed-out session_ids.
    """
    now = datetime.now(timezone.utc).isoformat()
    rows = conn.execute(
        """SELECT session_id FROM telegram_sessions
           WHERE status = 'active' AND timeout_at < ?""",
        (now,),
    ).fetchall()

    timed_out = [row["session_id"] for row in rows]
    if timed_out:
        conn.execute(
            f"""UPDATE telegram_sessions SET status = 'timed_out'
                WHERE status = 'active' AND timeout_at < ?""",
            (now,),
        )
        conn.commit()
    return timed_out


def get_active_sessions(conn: sqlite3.Connection) -> list[dict]:
    """Get all active sessions."""
    rows = conn.execute(
        "SELECT * FROM telegram_sessions WHERE status = 'active' ORDER BY started_at DESC"
    ).fetchall()
    return [dict(r) for r in rows]
