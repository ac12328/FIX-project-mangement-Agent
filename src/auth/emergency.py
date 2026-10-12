from __future__ import annotations

import sqlite3
import uuid
from datetime import timedelta
from typing import Any

from src.auth.repository import db_timestamp, record_security_event, utc_now

try:
    from argon2 import PasswordHasher
    from argon2.exceptions import InvalidHashError, VerifyMismatchError
except ImportError:  # pragma: no cover - dependency validation covers deployment
    PasswordHasher = None  # type: ignore[assignment]
    InvalidHashError = VerifyMismatchError = Exception  # type: ignore[assignment]


def hash_password_argon2id(password: str) -> str:
    if PasswordHasher is None:
        raise RuntimeError("argon2-cffi is required for break-glass authentication")
    return PasswordHasher(time_cost=3, memory_cost=65536, parallelism=4).hash(password)


def verify_password_argon2id(password: str, encoded: str) -> bool:
    if PasswordHasher is None:
        raise RuntimeError("argon2-cffi is required for break-glass authentication")
    try:
        return bool(PasswordHasher().verify(encoded, password))
    except (VerifyMismatchError, InvalidHashError, ValueError):
        return False


def initialize_break_glass(
    conn: sqlite3.Connection,
    *,
    username: str,
    password: str,
    display_name: str = "Break-glass Admin",
) -> dict[str, Any]:
    if len(password) < 16:
        raise ValueError("Break-glass password must be at least 16 characters")
    user_id = f"local:{uuid.uuid4()}"
    existing = conn.execute(
        "SELECT user_id FROM local_auth_credentials WHERE username=? COLLATE NOCASE",
        (username.strip(),),
    ).fetchone()
    if existing:
        raise ValueError("Break-glass account already exists")
    conn.execute(
        """
        INSERT INTO dashboard_users(
            user_id,display_name,role,status,account_type,is_primary_admin
        ) VALUES (?, ?, 'Admin', 'active', 'local', 0)
        """,
        (user_id, display_name.strip() or "Break-glass Admin"),
    )
    conn.execute(
        """
        INSERT INTO local_auth_credentials(
            user_id,username,password_hash,totp_ciphertext,totp_nonce,totp_key_version
        ) VALUES (?, ?, ?, ?, ?, 'disabled')
        """,
        (user_id, username.strip(), hash_password_argon2id(password), "", ""),
    )
    record_security_event(
        conn,
        "emergency.account_initialized",
        severity="critical",
        actor_user_id=user_id,
        subject_user_id=user_id,
    )
    return {
        "user_id": user_id,
        "username": username.strip(),
    }


def rotate_password(conn: sqlite3.Connection, *, username: str, password: str) -> None:
    if len(password) < 16:
        raise ValueError("Break-glass password must be at least 16 characters")
    row = _credential_row(conn, username)
    if not row:
        raise KeyError(username)
    conn.execute(
        """
        UPDATE local_auth_credentials
        SET password_hash=?, password_changed_at=datetime('now'), updated_at=datetime('now')
        WHERE user_id=?
        """,
        (hash_password_argon2id(password), row["user_id"]),
    )
    conn.execute(
        """
        UPDATE auth_sessions SET revoked_at=datetime('now'), revoke_reason='password_rotated'
        WHERE user_id=? AND revoked_at IS NULL
        """,
        (row["user_id"],),
    )
    record_security_event(
        conn,
        "emergency.password_rotated",
        severity="critical",
        actor_user_id=row["user_id"],
        subject_user_id=row["user_id"],
    )


def set_enabled(conn: sqlite3.Connection, *, username: str, enabled: bool) -> None:
    row = _credential_row(conn, username)
    if not row:
        raise KeyError(username)
    conn.execute(
        "UPDATE local_auth_credentials SET enabled=?,updated_at=datetime('now') WHERE user_id=?",
        (1 if enabled else 0, row["user_id"]),
    )
    conn.execute(
        "UPDATE dashboard_users SET status=?,updated_at=datetime('now') WHERE user_id=?",
        ("active" if enabled else "disabled", row["user_id"]),
    )
    if not enabled:
        conn.execute(
            """
            UPDATE auth_sessions SET revoked_at=datetime('now'),revoke_reason='emergency_disabled'
            WHERE user_id=? AND revoked_at IS NULL
            """,
            (row["user_id"],),
        )


def authenticate_emergency(
    conn: sqlite3.Connection,
    *,
    username: str,
    password: str,
    ip_address: str,
    user_agent: str | None = None,
) -> dict[str, Any]:
    account_key = username.strip().casefold()
    blocked_reason = check_rate_limit(conn, ip_address=ip_address, account_key=account_key)
    if blocked_reason:
        record_security_event(
            conn,
            "emergency.rate_limited",
            severity="critical" if blocked_reason == "global" else "warning",
            ip_address=ip_address,
            user_agent=user_agent,
            metadata={"bucket": blocked_reason},
        )
        raise PermissionError("Emergency login is temporarily rate limited")

    row = _credential_row(conn, username)
    password_ok = bool(row and row["enabled"] and row["status"] == "active")
    if password_ok:
        password_ok = verify_password_argon2id(password, row["password_hash"])
    else:
        _dummy_password_verify(password)

    if not password_ok or not row:
        register_failure(conn, ip_address=ip_address, account_key=account_key)
        record_security_event(
            conn,
            "emergency.login_failed",
            severity="warning",
            subject_user_id=row["user_id"] if row else None,
            ip_address=ip_address,
            user_agent=user_agent,
        )
        raise PermissionError("Invalid emergency credentials")

    clear_account_rate_limits(conn, ip_address=ip_address, account_key=account_key)
    conn.execute(
        "UPDATE dashboard_users SET last_login_at=datetime('now'),updated_at=datetime('now') WHERE user_id=?",
        (row["user_id"],),
    )
    record_security_event(
        conn,
        "emergency.login_succeeded",
        severity="critical",
        actor_user_id=row["user_id"],
        subject_user_id=row["user_id"],
        ip_address=ip_address,
        user_agent=user_agent,
        metadata={"factor": "password"},
    )
    return {
        "user_id": row["user_id"],
        "email": row["email"],
        "display_name": row["display_name"],
        "role": row["role"],
        "status": row["status"],
        "account_type": row["account_type"],
    }


def check_rate_limit(conn: sqlite3.Connection, *, ip_address: str, account_key: str) -> str | None:
    now = utc_now()
    for bucket_type, bucket_key in (
        ("global", "dashboard-emergency"),
        ("ip", ip_address or "unknown"),
        ("account", account_key or "unknown"),
    ):
        row = conn.execute(
            "SELECT blocked_until FROM auth_rate_limits WHERE bucket_type=? AND bucket_key=?",
            (bucket_type, bucket_key),
        ).fetchone()
        if row and row["blocked_until"] and row["blocked_until"] > db_timestamp(now):
            return bucket_type
    return None


def register_failure(conn: sqlite3.Connection, *, ip_address: str, account_key: str) -> None:
    now = utc_now()
    for bucket_type, bucket_key, window_minutes, threshold, block_minutes in (
        ("ip", ip_address or "unknown", 15, 5, 15),
        ("account", account_key or "unknown", 15, 5, 15),
        ("global", "dashboard-emergency", 60, 20, 30),
    ):
        row = conn.execute(
            "SELECT * FROM auth_rate_limits WHERE bucket_type=? AND bucket_key=?",
            (bucket_type, bucket_key),
        ).fetchone()
        window_start = now
        failure_count = 1
        if row:
            existing_start = _parse_time(row["window_started_at"])
            if now - existing_start < timedelta(minutes=window_minutes):
                window_start = existing_start
                failure_count = int(row["failure_count"]) + 1
        blocked_until = None
        if failure_count >= threshold:
            blocked_until = db_timestamp(now + timedelta(minutes=block_minutes))
        conn.execute(
            """
            INSERT INTO auth_rate_limits(
                bucket_type,bucket_key,window_started_at,failure_count,blocked_until,updated_at
            ) VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(bucket_type,bucket_key) DO UPDATE SET
                window_started_at=excluded.window_started_at,
                failure_count=excluded.failure_count,
                blocked_until=excluded.blocked_until,
                updated_at=excluded.updated_at
            """,
            (
                bucket_type, bucket_key, db_timestamp(window_start), failure_count,
                blocked_until, db_timestamp(now),
            ),
        )


def clear_account_rate_limits(conn: sqlite3.Connection, *, ip_address: str, account_key: str) -> None:
    conn.execute(
        """
        DELETE FROM auth_rate_limits
        WHERE (bucket_type='ip' AND bucket_key=?)
           OR (bucket_type='account' AND bucket_key=?)
        """,
        (ip_address or "unknown", account_key or "unknown"),
    )


def _credential_row(conn: sqlite3.Connection, username: str) -> sqlite3.Row | None:
    return conn.execute(
        """
        SELECT c.*,u.email,u.display_name,u.role,u.status,u.account_type
        FROM local_auth_credentials c JOIN dashboard_users u ON u.user_id=c.user_id
        WHERE c.username=? COLLATE NOCASE
        """,
        (username.strip(),),
    ).fetchone()


def _dummy_password_verify(password: str) -> None:
    # Keep unknown-account timing closer to a real Argon2 verification.
    dummy = "$argon2id$v=19$m=65536,t=3,p=4$MDEyMzQ1Njc4OWFiY2RlZg$P6CPGz6XJnPsxYhIu85uV7OFQoQIbuL9MiJ3bqrx90U"
    try:
        verify_password_argon2id(password, dummy)
    except RuntimeError:
        raise


def _parse_time(value: str):
    from datetime import datetime, timezone

    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return parsed.replace(tzinfo=parsed.tzinfo or timezone.utc).astimezone(timezone.utc)
