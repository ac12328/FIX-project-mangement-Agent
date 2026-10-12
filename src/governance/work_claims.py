"""Soft TTL claims so two Editors do not silently share the same draft."""

from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone

from config import defaults


class ClaimConflictError(RuntimeError):
    def __init__(self, claimed_by: str, claimed_at: str | None):
        super().__init__(f"Draft is claimed by {claimed_by}")
        self.claimed_by = claimed_by
        self.claimed_at = claimed_at


def _ttl_seconds() -> int:
    return int(getattr(defaults, "CLAIM_TTL_SECONDS", 15 * 60) or 15 * 60)


def _expired(claimed_at: str | None) -> bool:
    if not claimed_at:
        return True
    try:
        stamp = datetime.fromisoformat(claimed_at.rstrip("Z")).replace(tzinfo=timezone.utc)
    except ValueError:
        return True
    return datetime.now(timezone.utc) - stamp > timedelta(seconds=_ttl_seconds())


def get_draft_claim(conn: sqlite3.Connection, draft_id: str) -> dict[str, str | None] | None:
    row = conn.execute(
        "SELECT claimed_by, claimed_at FROM drafts WHERE draft_id=?",
        (draft_id,),
    ).fetchone()
    if row is None:
        return None
    claimed_by = row["claimed_by"]
    claimed_at = row["claimed_at"]
    if not claimed_by or _expired(claimed_at):
        return None
    return {"claimed_by": claimed_by, "claimed_at": claimed_at}


def claim_draft(conn: sqlite3.Connection, draft_id: str, actor: str) -> dict[str, str | None]:
    current = get_draft_claim(conn, draft_id)
    if current and current["claimed_by"] != actor:
        raise ClaimConflictError(current["claimed_by"] or "", current.get("claimed_at"))
    now = datetime.now(timezone.utc).isoformat()
    conn.execute(
        "UPDATE drafts SET claimed_by=?, claimed_at=? WHERE draft_id=?",
        (actor, now, draft_id),
    )
    return {"claimed_by": actor, "claimed_at": now}


def release_draft_claim(conn: sqlite3.Connection, draft_id: str, actor: str) -> None:
    conn.execute(
        """UPDATE drafts SET claimed_by=NULL, claimed_at=NULL
           WHERE draft_id=? AND claimed_by=?""",
        (draft_id, actor),
    )
