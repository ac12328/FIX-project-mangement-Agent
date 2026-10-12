"""Local Dashboard registration, password authentication, and action tokens."""

from __future__ import annotations

import secrets
import sqlite3
import uuid
from dataclasses import dataclass
from datetime import timedelta
from typing import Any

from config.settings import CONFIG, MIN_DASHBOARD_PASSWORD_LENGTH
from src.auth.emergency import hash_password_argon2id, verify_password_argon2id
from src.auth.repository import db_timestamp, get_user, record_security_event, utc_now
from src.auth.sessions import hash_secret

EMAIL_VERIFICATION = "email_verification"
PASSWORD_RESET = "password_reset"


@dataclass(frozen=True)
class ActionDelivery:
    action_type: str
    email: str
    display_name: str
    token: str


def allowed_email_domains() -> tuple[str, ...]:
    domains = {
        domain.strip().casefold().lstrip("@")
        for domain in CONFIG.dashboard_auth.allowed_email_domains
        if domain.strip()
    }
    return tuple(sorted(domains))


def normalize_corporate_email(email: str) -> str:
    normalized = email.strip().casefold()
    if len(normalized) > 254 or normalized.count("@") != 1:
        raise ValueError(_domain_error())
    local_part, domain = normalized.rsplit("@", 1)
    if not local_part or not domain or any(char.isspace() for char in normalized):
        raise ValueError(_domain_error())
    if domain not in allowed_email_domains():
        raise ValueError(_domain_error())
    return normalized


def validate_password(password: str) -> None:
    minimum = max(
        MIN_DASHBOARD_PASSWORD_LENGTH, int(CONFIG.dashboard_auth.password_min_length)
    )
    if len(password) < minimum:
        raise ValueError(f"Password must be at least {minimum} characters")
    if len(password) > 1000:
        raise ValueError("Password is too long")


def register_local_user(
    conn: sqlite3.Connection,
    *,
    email: str,
    password: str,
    display_name: str,
    ip_address: str | None = None,
    user_agent: str | None = None,
) -> ActionDelivery | None:
    if CONFIG.dashboard_auth.mode != "local" or not CONFIG.dashboard_auth.local_registration_enabled:
        raise PermissionError("Local registration is disabled")
    normalized_email = normalize_corporate_email(email)
    validate_password(password)
    normalized_name = display_name.strip() or normalized_email.split("@", 1)[0]
    if len(normalized_name) > 200:
        raise ValueError("Display name is too long")

    # Hash before account lookup so duplicate registration has similar timing and
    # can never be used to replace an existing account's password.
    password_hash = hash_password_argon2id(password)
    existing = _find_local_account(conn, normalized_email)
    if existing:
        if existing["status"] == "pending_verification" and int(existing["enabled"]):
            return _issue_action_token(
                conn,
                user=existing,
                action_type=EMAIL_VERIFICATION,
                ttl_minutes=CONFIG.dashboard_auth.email_verification_ttl_minutes,
                requested_ip=ip_address,
                cooldown_seconds=60,
            )
        return None

    duplicate = conn.execute(
        "SELECT * FROM dashboard_users WHERE email=? COLLATE NOCASE",
        (normalized_email,),
    ).fetchone()
    if duplicate:
        configured_admin_email = (CONFIG.dashboard_auth.initial_admin_email or "").strip().casefold()
        is_claimable_primary = bool(
            int(duplicate["is_primary_admin"])
            and normalized_email == configured_admin_email
            and not conn.execute(
                "SELECT 1 FROM external_identities WHERE user_id=? LIMIT 1",
                (duplicate["user_id"],),
            ).fetchone()
            and not conn.execute(
                "SELECT 1 FROM local_auth_credentials WHERE user_id=? LIMIT 1",
                (duplicate["user_id"],),
            ).fetchone()
        )
        if not is_claimable_primary:
            return None
        user_id = str(duplicate["user_id"])
        conn.execute(
            """
            UPDATE dashboard_users
            SET email=?,display_name=?,status='pending_verification',
                account_type='local',email_verified_at=NULL,updated_at=datetime('now')
            WHERE user_id=?
            """,
            (normalized_email, normalized_name, user_id),
        )
    else:
        user_id = f"local:{uuid.uuid4()}"
        conn.execute(
            """
            INSERT INTO dashboard_users(
                user_id,email,display_name,role,status,account_type,is_primary_admin
            ) VALUES (?, ?, ?, 'Viewer', 'pending_verification', 'local', 0)
            """,
            (user_id, normalized_email, normalized_name),
        )

    conn.execute(
        """
        INSERT INTO local_auth_credentials(
            user_id,username,password_hash,totp_ciphertext,totp_nonce,totp_key_version,enabled
        ) VALUES (?, ?, ?, '', '', 'disabled', 1)
        """,
        (user_id, normalized_email, password_hash),
    )
    record_security_event(
        conn,
        "local.registration_created",
        actor_user_id=user_id,
        subject_user_id=user_id,
        ip_address=ip_address,
        user_agent=user_agent,
    )
    user = get_user(conn, user_id) or {}
    return _issue_action_token(
        conn,
        user=user,
        action_type=EMAIL_VERIFICATION,
        ttl_minutes=CONFIG.dashboard_auth.email_verification_ttl_minutes,
        requested_ip=ip_address,
    )


def authenticate_local(
    conn: sqlite3.Connection,
    *,
    email: str,
    password: str,
    ip_address: str | None = None,
    user_agent: str | None = None,
) -> dict[str, Any]:
    try:
        normalized_email = normalize_corporate_email(email)
    except ValueError:
        _dummy_password_verify(password)
        _register_login_failure(conn, ip_address=ip_address, account_key=email.strip().casefold())
        raise PermissionError("Invalid email or password") from None

    blocked_bucket = _check_login_rate_limit(
        conn, ip_address=ip_address, account_key=normalized_email
    )
    if blocked_bucket:
        record_security_event(
            conn,
            "local.login_rate_limited",
            severity="warning",
            ip_address=ip_address,
            user_agent=user_agent,
            metadata={"bucket_type": blocked_bucket},
        )
        raise PermissionError("Login is temporarily rate limited")

    row = _find_local_account(conn, normalized_email)
    if not row:
        _dummy_password_verify(password)
        _register_login_failure(conn, ip_address=ip_address, account_key=normalized_email)
        raise PermissionError("Invalid email or password")
    if not verify_password_argon2id(password, row["password_hash"]):
        _register_login_failure(conn, ip_address=ip_address, account_key=normalized_email)
        record_security_event(
            conn,
            "local.login_failed",
            severity="warning",
            subject_user_id=row["user_id"],
            ip_address=ip_address,
            user_agent=user_agent,
        )
        raise PermissionError("Invalid email or password")
    if not int(row["enabled"]):
        raise PermissionError("Invalid email or password")
    if row["status"] == "pending_verification":
        raise PermissionError("Verify your email before signing in")
    if row["status"] != "active":
        raise PermissionError("User account is disabled")

    _clear_login_rate_limits(conn, ip_address=ip_address, account_key=normalized_email)
    conn.execute(
        "UPDATE dashboard_users SET last_login_at=datetime('now'),updated_at=datetime('now') WHERE user_id=?",
        (row["user_id"],),
    )
    record_security_event(
        conn,
        "local.login_succeeded",
        actor_user_id=row["user_id"],
        subject_user_id=row["user_id"],
        ip_address=ip_address,
        user_agent=user_agent,
    )
    return get_user(conn, row["user_id"]) or dict(row)


def resend_verification(
    conn: sqlite3.Connection,
    *,
    email: str,
    ip_address: str | None = None,
) -> ActionDelivery | None:
    try:
        normalized_email = normalize_corporate_email(email)
    except ValueError:
        return None
    user = _find_local_account(conn, normalized_email)
    if not user or user["status"] != "pending_verification" or not int(user["enabled"]):
        return None
    return _issue_action_token(
        conn,
        user=user,
        action_type=EMAIL_VERIFICATION,
        ttl_minutes=CONFIG.dashboard_auth.email_verification_ttl_minutes,
        requested_ip=ip_address,
        cooldown_seconds=60,
    )


def verify_email_token(conn: sqlite3.Connection, token: str) -> dict[str, Any]:
    row = _action_token_row(conn, token, EMAIL_VERIFICATION)
    if not row:
        raise ValueError("Verification link is invalid or expired")
    if row["status"] == "disabled":
        conn.execute(
            "UPDATE auth_action_tokens SET used_at=datetime('now') WHERE token_id=?",
            (row["token_id"],),
        )
        record_security_event(
            conn,
            "local.email_verification_blocked",
            severity="warning",
            subject_user_id=row["user_id"],
        )
        raise PermissionError("User account is disabled")
    if row["status"] != "pending_verification":
        raise ValueError("Verification link is invalid or expired")

    conn.execute(
        "UPDATE auth_action_tokens SET used_at=datetime('now') WHERE token_id=? AND used_at IS NULL",
        (row["token_id"],),
    )
    conn.execute(
        """
        UPDATE dashboard_users
        SET status='active',email_verified_at=datetime('now'),updated_at=datetime('now')
        WHERE user_id=? AND status='pending_verification'
        """,
        (row["user_id"],),
    )
    record_security_event(
        conn,
        "local.email_verified",
        actor_user_id=row["user_id"],
        subject_user_id=row["user_id"],
    )
    return get_user(conn, row["user_id"]) or dict(row)


def request_password_reset(
    conn: sqlite3.Connection,
    *,
    email: str,
    ip_address: str | None = None,
) -> ActionDelivery | None:
    try:
        normalized_email = normalize_corporate_email(email)
    except ValueError:
        return None
    user = _find_local_account(conn, normalized_email)
    if (
        not user
        or user["status"] != "active"
        or not user["email_verified_at"]
        or not int(user["enabled"])
    ):
        return None
    return _issue_action_token(
        conn,
        user=user,
        action_type=PASSWORD_RESET,
        ttl_minutes=CONFIG.dashboard_auth.password_reset_ttl_minutes,
        requested_ip=ip_address,
        cooldown_seconds=60,
    )


def issue_admin_password_reset(
    conn: sqlite3.Connection,
    *,
    user_id: str,
    requested_ip: str | None = None,
) -> ActionDelivery:
    user = _find_local_account_by_user_id(conn, user_id)
    if not user:
        raise ValueError("Password reset is only available for local accounts")
    if user["status"] != "active" or not user["email_verified_at"]:
        raise ValueError("User must have an active verified email address")
    return _issue_action_token(
        conn,
        user=user,
        action_type=PASSWORD_RESET,
        ttl_minutes=CONFIG.dashboard_auth.password_reset_ttl_minutes,
        requested_ip=requested_ip,
    )


def reset_password(conn: sqlite3.Connection, *, token: str, password: str) -> dict[str, Any]:
    validate_password(password)
    row = _action_token_row(conn, token, PASSWORD_RESET)
    if not row:
        raise ValueError("Password reset link is invalid or expired")
    if row["status"] != "active" or not int(row["enabled"]):
        raise PermissionError("User account is disabled")

    password_hash = hash_password_argon2id(password)
    conn.execute(
        """
        UPDATE local_auth_credentials
        SET password_hash=?,password_changed_at=datetime('now'),updated_at=datetime('now')
        WHERE user_id=?
        """,
        (password_hash, row["user_id"]),
    )
    conn.execute(
        """
        UPDATE auth_action_tokens SET used_at=datetime('now')
        WHERE user_id=? AND action_type='password_reset' AND used_at IS NULL
        """,
        (row["user_id"],),
    )
    sessions = conn.execute(
        "SELECT session_id FROM auth_sessions WHERE user_id=? AND revoked_at IS NULL",
        (row["user_id"],),
    ).fetchall()
    conn.execute(
        """
        UPDATE auth_sessions SET revoked_at=datetime('now'),revoke_reason='password_reset'
        WHERE user_id=? AND revoked_at IS NULL
        """,
        (row["user_id"],),
    )
    for session in sessions:
        record_security_event(
            conn,
            "session.revoked",
            actor_user_id=row["user_id"],
            subject_user_id=row["user_id"],
            session_id=session["session_id"],
            metadata={"reason": "password_reset"},
        )
    record_security_event(
        conn,
        "local.password_reset_completed",
        severity="warning",
        actor_user_id=row["user_id"],
        subject_user_id=row["user_id"],
    )
    return get_user(conn, row["user_id"]) or dict(row)


def _issue_action_token(
    conn: sqlite3.Connection,
    *,
    user: dict[str, Any] | sqlite3.Row,
    action_type: str,
    ttl_minutes: int,
    requested_ip: str | None,
    cooldown_seconds: int = 0,
) -> ActionDelivery | None:
    user_id = str(user["user_id"])
    if cooldown_seconds:
        recent = conn.execute(
            """
            SELECT 1 FROM auth_action_tokens
            WHERE user_id=? AND action_type=? AND used_at IS NULL
              AND created_at > datetime('now', ?)
            LIMIT 1
            """,
            (user_id, action_type, f"-{max(1, cooldown_seconds)} seconds"),
        ).fetchone()
        if recent:
            return None

    now = utc_now()
    token = secrets.token_urlsafe(48)
    conn.execute(
        """
        UPDATE auth_action_tokens SET used_at=?
        WHERE user_id=? AND action_type=? AND used_at IS NULL
        """,
        (db_timestamp(now), user_id, action_type),
    )
    conn.execute(
        """
        INSERT INTO auth_action_tokens(
            token_id,user_id,action_type,token_hash,created_at,expires_at,requested_ip
        ) VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (
            str(uuid.uuid4()),
            user_id,
            action_type,
            hash_secret(token),
            db_timestamp(now),
            db_timestamp(now + timedelta(minutes=max(5, int(ttl_minutes)))),
            requested_ip,
        ),
    )
    record_security_event(
        conn,
        f"local.{action_type}_requested",
        actor_user_id=user_id,
        subject_user_id=user_id,
        ip_address=requested_ip,
    )
    return ActionDelivery(
        action_type=action_type,
        email=str(user["email"]),
        display_name=str(user["display_name"]),
        token=token,
    )


def _action_token_row(
    conn: sqlite3.Connection, token: str, action_type: str
) -> sqlite3.Row | None:
    if not token or len(token) > 500:
        return None
    return conn.execute(
        """
        SELECT t.*,u.email,u.display_name,u.status,u.account_type,
               c.enabled,c.password_hash
        FROM auth_action_tokens t
        JOIN dashboard_users u ON u.user_id=t.user_id
        JOIN local_auth_credentials c ON c.user_id=t.user_id
        WHERE t.token_hash=? AND t.action_type=? AND t.used_at IS NULL
          AND t.expires_at > datetime('now')
        """,
        (hash_secret(token), action_type),
    ).fetchone()


def _find_local_account(conn: sqlite3.Connection, email: str) -> sqlite3.Row | None:
    return conn.execute(
        """
        SELECT u.*,c.username,c.password_hash,c.enabled
        FROM local_auth_credentials c
        JOIN dashboard_users u ON u.user_id=c.user_id
        WHERE c.username=? COLLATE NOCASE AND u.account_type='local'
        """,
        (email,),
    ).fetchone()


def _find_local_account_by_user_id(
    conn: sqlite3.Connection, user_id: str
) -> sqlite3.Row | None:
    return conn.execute(
        """
        SELECT u.*,c.username,c.password_hash,c.enabled
        FROM local_auth_credentials c
        JOIN dashboard_users u ON u.user_id=c.user_id
        WHERE c.user_id=? AND u.account_type='local'
        """,
        (user_id,),
    ).fetchone()


def _check_login_rate_limit(
    conn: sqlite3.Connection, *, ip_address: str | None, account_key: str
) -> str | None:
    now = db_timestamp()
    for bucket_type, bucket_key in _login_buckets(ip_address, account_key):
        row = conn.execute(
            "SELECT blocked_until FROM auth_rate_limits WHERE bucket_type=? AND bucket_key=?",
            (bucket_type, bucket_key),
        ).fetchone()
        if row and row["blocked_until"] and row["blocked_until"] > now:
            return bucket_type
    return None


def _register_login_failure(
    conn: sqlite3.Connection, *, ip_address: str | None, account_key: str
) -> None:
    now = utc_now()
    policies = (
        ("ip", f"local:{ip_address or 'unknown'}", 15, 5, 15),
        ("account", f"local:{account_key or 'unknown'}", 15, 5, 15),
        ("global", "dashboard-local", 60, 50, 15),
    )
    for bucket_type, bucket_key, window_minutes, threshold, block_minutes in policies:
        row = conn.execute(
            "SELECT * FROM auth_rate_limits WHERE bucket_type=? AND bucket_key=?",
            (bucket_type, bucket_key),
        ).fetchone()
        window_started_at = now
        failure_count = 1
        if row:
            existing_start = _parse_time(row["window_started_at"])
            if now - existing_start < timedelta(minutes=window_minutes):
                window_started_at = existing_start
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
                bucket_type,
                bucket_key,
                db_timestamp(window_started_at),
                failure_count,
                blocked_until,
                db_timestamp(now),
            ),
        )


def _clear_login_rate_limits(
    conn: sqlite3.Connection, *, ip_address: str | None, account_key: str
) -> None:
    buckets = _login_buckets(ip_address, account_key)
    conn.executemany(
        "DELETE FROM auth_rate_limits WHERE bucket_type=? AND bucket_key=?",
        [bucket for bucket in buckets if bucket[0] != "global"],
    )


def _login_buckets(
    ip_address: str | None, account_key: str
) -> tuple[tuple[str, str], ...]:
    return (
        ("ip", f"local:{ip_address or 'unknown'}"),
        ("account", f"local:{account_key or 'unknown'}"),
        ("global", "dashboard-local"),
    )


def _dummy_password_verify(password: str) -> None:
    dummy = (
        "$argon2id$v=19$m=65536,t=3,p=4$MDEyMzQ1Njc4OWFiY2RlZg$"
        "P6CPGz6XJnPsxYhIu85uV7OFQoQIbuL9MiJ3bqrx90U"
    )
    verify_password_argon2id(password, dummy)


def _parse_time(value: str):
    from datetime import datetime, timezone

    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return parsed.replace(tzinfo=parsed.tzinfo or timezone.utc).astimezone(timezone.utc)


def _domain_error() -> str:
    domains = allowed_email_domains()
    suffix = " or ".join(f"@{domain}" for domain in domains) or "an approved company domain"
    return f"Use your {suffix} email address"
