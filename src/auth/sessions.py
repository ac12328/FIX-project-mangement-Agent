from __future__ import annotations

import hashlib
import hmac
import secrets
import sqlite3
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

from config.settings import CONFIG
from src.auth.repository import db_timestamp, record_security_event, utc_now


@dataclass(frozen=True)
class SessionMaterial:
    session_id: str
    session_token: str
    csrf_token: str
    absolute_expires_at: str


def hash_secret(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def create_session(
    conn: sqlite3.Connection,
    *,
    user_id: str,
    auth_method: str,
    ip_address: str | None = None,
    user_agent: str | None = None,
) -> SessionMaterial:
    now = utc_now()
    absolute = now + timedelta(hours=max(1, CONFIG.dashboard_auth.absolute_session_hours))
    idle = min(
        absolute,
        now + timedelta(minutes=max(5, CONFIG.dashboard_auth.idle_session_minutes)),
    )
    session_id = str(uuid.uuid4())
    session_token = secrets.token_urlsafe(48)
    csrf_token = secrets.token_urlsafe(32)
    conn.execute(
        """
        INSERT INTO auth_sessions(
            session_id,user_id,token_hash,csrf_hash,auth_method,ip_address,user_agent,
            created_at,last_seen_at,idle_expires_at,absolute_expires_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            session_id, user_id, hash_secret(session_token), hash_secret(csrf_token),
            auth_method, ip_address, user_agent, db_timestamp(now), db_timestamp(now),
            db_timestamp(idle), db_timestamp(absolute),
        ),
    )
    record_security_event(
        conn,
        "session.created",
        actor_user_id=user_id,
        subject_user_id=user_id,
        session_id=session_id,
        ip_address=ip_address,
        user_agent=user_agent,
        metadata={"auth_method": auth_method},
    )
    return SessionMaterial(session_id, session_token, csrf_token, db_timestamp(absolute))


def authenticate_session(
    conn: sqlite3.Connection,
    session_token: str | None,
    *,
    touch: bool = True,
) -> dict[str, Any] | None:
    if not session_token:
        return None
    row = conn.execute(
        """
        SELECT s.*, u.email, u.display_name, u.role, u.status, u.account_type
        FROM auth_sessions s JOIN dashboard_users u ON u.user_id=s.user_id
        WHERE s.token_hash=?
        """,
        (hash_secret(session_token),),
    ).fetchone()
    if not row:
        return None
    session = dict(row)
    if session["revoked_at"]:
        return None

    now = utc_now()
    absolute = _parse_db_time(session["absolute_expires_at"])
    idle = _parse_db_time(session["idle_expires_at"])
    if now >= absolute or now >= idle:
        if touch:
            conn.execute(
                """
                UPDATE auth_sessions SET revoked_at=?, revoke_reason=?
                WHERE session_id=? AND revoked_at IS NULL
                """,
                (
                    db_timestamp(now),
                    "absolute_expired" if now >= absolute else "idle_expired",
                    session["session_id"],
                ),
            )
        return None
    if session["status"] != "active":
        if touch:
            conn.execute(
                """
                UPDATE auth_sessions SET revoked_at=?, revoke_reason='user_disabled'
                WHERE session_id=? AND revoked_at IS NULL
                """,
                (db_timestamp(now), session["session_id"]),
            )
        return None

    last_seen = _parse_db_time(session["last_seen_at"])
    touch_after = timedelta(minutes=max(1, CONFIG.dashboard_auth.session_touch_minutes))
    if touch and now - last_seen >= touch_after:
        next_idle = min(
            absolute,
            now + timedelta(minutes=max(5, CONFIG.dashboard_auth.idle_session_minutes)),
        )
        conn.execute(
            "UPDATE auth_sessions SET last_seen_at=?, idle_expires_at=? WHERE session_id=?",
            (db_timestamp(now), db_timestamp(next_idle), session["session_id"]),
        )
        session["last_seen_at"] = db_timestamp(now)
        session["idle_expires_at"] = db_timestamp(next_idle)
    return session


def touch_session(conn: sqlite3.Connection, session_id: str) -> bool:
    """Refresh one active session from the asynchronous request-side touch worker."""
    row = conn.execute(
        """
        SELECT s.session_id,s.last_seen_at,s.idle_expires_at,s.absolute_expires_at,
               s.revoked_at,u.status
        FROM auth_sessions s JOIN dashboard_users u ON u.user_id=s.user_id
        WHERE s.session_id=?
        """,
        (session_id,),
    ).fetchone()
    if not row or row["revoked_at"]:
        return False

    now = utc_now()
    absolute = _parse_db_time(row["absolute_expires_at"])
    idle = _parse_db_time(row["idle_expires_at"])
    if now >= absolute or now >= idle or row["status"] != "active":
        reason = (
            "absolute_expired"
            if now >= absolute
            else "idle_expired"
            if now >= idle
            else "user_disabled"
        )
        conn.execute(
            """
            UPDATE auth_sessions SET revoked_at=?, revoke_reason=?
            WHERE session_id=? AND revoked_at IS NULL
            """,
            (db_timestamp(now), reason, session_id),
        )
        return False

    last_seen = _parse_db_time(row["last_seen_at"])
    touch_after = timedelta(minutes=max(1, CONFIG.dashboard_auth.session_touch_minutes))
    if now - last_seen < touch_after:
        return True
    next_idle = min(
        absolute,
        now + timedelta(minutes=max(5, CONFIG.dashboard_auth.idle_session_minutes)),
    )
    conn.execute(
        "UPDATE auth_sessions SET last_seen_at=?, idle_expires_at=? WHERE session_id=?",
        (db_timestamp(now), db_timestamp(next_idle), session_id),
    )
    return True


def validate_csrf_tokens(
    conn: sqlite3.Connection,
    *,
    session_id: str | None,
    supplied_token: str | None,
    cookie_token: str | None,
) -> bool:
    if not session_id or not supplied_token or not cookie_token:
        return False
    if not hmac.compare_digest(supplied_token, cookie_token):
        return False
    row = conn.execute(
        "SELECT csrf_hash, revoked_at FROM auth_sessions WHERE session_id=?",
        (session_id,),
    ).fetchone()
    return bool(
        row
        and not row["revoked_at"]
        and hmac.compare_digest(hash_secret(supplied_token), row["csrf_hash"])
    )


def revoke_session(
    conn: sqlite3.Connection,
    session_id: str,
    *,
    actor_user_id: str,
    allow_admin: bool = False,
    reason: str = "user_logout",
) -> bool:
    row = conn.execute(
        "SELECT user_id, revoked_at FROM auth_sessions WHERE session_id=?", (session_id,)
    ).fetchone()
    if not row:
        return False
    if row["user_id"] != actor_user_id and not allow_admin:
        raise PermissionError("Cannot revoke another user's session")
    if not row["revoked_at"]:
        conn.execute(
            "UPDATE auth_sessions SET revoked_at=datetime('now'), revoke_reason=? WHERE session_id=?",
            (reason, session_id),
        )
        record_security_event(
            conn,
            "session.revoked",
            actor_user_id=actor_user_id,
            subject_user_id=row["user_id"],
            session_id=session_id,
            metadata={"reason": reason},
        )
    return True


def revoke_all_sessions(
    conn: sqlite3.Connection,
    *,
    user_id: str,
    actor_user_id: str,
    reason: str = "logout_all",
) -> int:
    rows = conn.execute(
        "SELECT session_id FROM auth_sessions WHERE user_id=? AND revoked_at IS NULL",
        (user_id,),
    ).fetchall()
    conn.execute(
        """
        UPDATE auth_sessions SET revoked_at=datetime('now'), revoke_reason=?
        WHERE user_id=? AND revoked_at IS NULL
        """,
        (reason, user_id),
    )
    for row in rows:
        record_security_event(
            conn,
            "session.revoked",
            actor_user_id=actor_user_id,
            subject_user_id=user_id,
            session_id=row["session_id"],
            metadata={"reason": reason},
        )
    return len(rows)


def list_sessions(
    conn: sqlite3.Connection,
    *,
    user_id: str,
    current_session_id: str | None = None,
    include_terminated: bool = False,
) -> list[dict[str, Any]]:
    where = "user_id=?" if include_terminated else "user_id=? AND revoked_at IS NULL"
    rows = conn.execute(
        f"""
        SELECT session_id,auth_method,ip_address,user_agent,created_at,last_seen_at,
               idle_expires_at,absolute_expires_at,revoked_at,revoke_reason
        FROM auth_sessions WHERE {where} ORDER BY created_at DESC
        """,
        (user_id,),
    ).fetchall()
    result: list[dict[str, Any]] = []
    now = utc_now()
    for row in rows:
        item = dict(row)
        item["current"] = item["session_id"] == current_session_id
        item["active"] = bool(
            not item["revoked_at"]
            and _parse_db_time(item["idle_expires_at"]) > now
            and _parse_db_time(item["absolute_expires_at"]) > now
        )
        result.append(item)
    return result


def _parse_db_time(value: str) -> datetime:
    raw = value.strip().replace("Z", "+00:00")
    parsed = datetime.fromisoformat(raw)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)
