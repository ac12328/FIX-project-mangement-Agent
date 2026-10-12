"""
Advisory scheduled jobs — resolution follow-up checks.
"""

import json
import logging
import sqlite3
from datetime import datetime, timedelta, timezone

from src.telegram.i18n import t

logger = logging.getLogger(__name__)


def _resolution_reply_timeout_minutes() -> int:
    from config.settings import CONFIG
    return max(30, int(getattr(CONFIG.thresholds, "resolution_reply_timeout_minutes", 1440)))


def check_resolution_followups(conn: sqlite3.Connection) -> list[dict]:
    """Find resolution_learning sessions past their handle delay and push follow-ups.

    Sessions may be ``active`` or ``timed_out`` (session_timeout job can fire first).
    After the follow-up is queued, the session is reactivated with a longer reply window.
    """
    now_dt = datetime.now(timezone.utc)
    now = now_dt.isoformat()
    rows = conn.execute(
        """SELECT session_id, originating_ref, intermediate_data, operator_id
           FROM telegram_sessions
           WHERE flow_type = 'resolution_learning'
             AND status IN ('active', 'timed_out')
             AND timeout_at <= ?""",
        (now,),
    ).fetchall()

    followups: list[dict] = []
    reply_timeout = _resolution_reply_timeout_minutes()
    for row in rows:
        data: dict = {}
        if row["intermediate_data"]:
            try:
                data = json.loads(row["intermediate_data"])
            except (json.JSONDecodeError, TypeError):
                data = {}

        if data.get("followup_sent"):
            continue

        ticket_id = data.get("ticket_id")
        project_id = data.get("project_id")
        work_type = data.get("work_type", "unknown")
        ref = row["originating_ref"] or ticket_id or project_id or "unknown"

        followups.append({
            "session_id": row["session_id"],
            "ref": ref,
            "ticket_id": ticket_id,
            "project_id": project_id,
            "work_type": work_type,
            "operator_id": row["operator_id"],
            "message": _build_followup_message(work_type, ref),
        })

        data["followup_sent"] = True
        data["followup_sent_at"] = now
        new_timeout = (now_dt + timedelta(minutes=reply_timeout)).isoformat()
        conn.execute(
            """UPDATE telegram_sessions
               SET status = 'active', timeout_at = ?, last_action_at = ?,
                   intermediate_data = ?
               WHERE session_id = ?""",
            (new_timeout, now, json.dumps(data, ensure_ascii=False), row["session_id"]),
        )

    if followups:
        conn.commit()
        logger.info("Resolution followup: %d session(s) awaiting operator reply", len(followups))
    return followups


def _build_followup_message(work_type: str, ref: str) -> str:
    """Build the follow-up question text (locale via i18n t())."""
    if work_type == "support":
        return t("advisory.resolution_followup_support").format(ref=ref)
    return t("advisory.resolution_followup_project").format(ref=ref)