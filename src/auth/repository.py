from __future__ import annotations

import json
import sqlite3
import uuid
from datetime import datetime, timezone
from typing import Any

from config import settings as config_settings
from src.auth.rbac import ROLE_ADMIN, ROLE_VIEWER, VALID_ROLES, VALID_STATUSES

TELEGRAM_IDENTITY_PROVIDER = "telegram"
TELEGRAM_IDENTITY_TENANT = "telegram"

_SENSITIVE_METADATA_KEYS = {
    "password", "password_hash", "totp", "totp_secret", "recovery_code",
    "access_token", "refresh_token", "id_token", "client_secret", "code_verifier",
    "nonce", "state", "secret", "token",
}


def utc_now() -> datetime:
    return datetime.now(timezone.utc).replace(microsecond=0)


def db_timestamp(value: datetime | None = None) -> str:
    current = value or utc_now()
    return current.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def bootstrap_primary_admin(conn: sqlite3.Connection) -> dict[str, Any]:
    """Create the operator fallback Admin without claiming an external identity."""
    user_id = config_settings.CONFIG.operator_id or "default_operator"
    email = (config_settings.CONFIG.dashboard_auth.initial_admin_email or "").strip() or None
    display_name = email or config_settings.CONFIG.dashboard_auth.username or "Primary Admin"
    account_type = "sso" if config_settings.CONFIG.dashboard_auth.mode == "entra" else "local"
    conn.execute(
        """
        INSERT INTO dashboard_users(
            user_id,email,display_name,role,status,account_type,is_primary_admin
        ) VALUES (?, ?, ?, 'Admin', 'active', ?, 1)
        ON CONFLICT(user_id) DO UPDATE SET
            is_primary_admin=1,
            updated_at=datetime('now')
        """,
        (user_id, email, display_name, account_type),
    )
    if config_settings.CONFIG.dashboard_auth.mode == "local":
        # A deployment moving away from Entra may already have an unbound SSO
        # placeholder for the primary operator. Reconcile only that unclaimed
        # placeholder so the configured corporate email can register normally.
        external_identity = conn.execute(
            "SELECT 1 FROM external_identities WHERE user_id=? LIMIT 1",
            (user_id,),
        ).fetchone()
        local_credential = conn.execute(
            "SELECT 1 FROM local_auth_credentials WHERE user_id=? LIMIT 1",
            (user_id,),
        ).fetchone()
        if not external_identity and not local_credential:
            conn.execute(
                """
                UPDATE dashboard_users
                SET email=COALESCE(?, email),
                    display_name=CASE WHEN ? IS NOT NULL THEN ? ELSE display_name END,
                    account_type='local',
                    updated_at=datetime('now')
                WHERE user_id=? AND is_primary_admin=1
                """,
                (email, email, display_name, user_id),
            )

    cursor = conn.execute("SELECT * FROM dashboard_users WHERE user_id=?", (user_id,))
    row = cursor.fetchone()
    if row is None:
        raise RuntimeError("Primary Dashboard Admin bootstrap did not create a user")
    columns = [description[0] for description in cursor.description or ()]
    return {column: row[index] for index, column in enumerate(columns)}


def get_user(conn: sqlite3.Connection, user_id: str) -> dict[str, Any] | None:
    row = conn.execute("SELECT * FROM dashboard_users WHERE user_id=?", (user_id,)).fetchone()
    return _with_telegram_identity(conn, dict(row) if row else None)


def find_user_by_identity(
    conn: sqlite3.Connection, tenant_id: str, object_id: str
) -> dict[str, Any] | None:
    return find_user_by_external_identity(
        conn,
        provider="entra",
        tenant_id=tenant_id,
        object_id=object_id,
    )


def find_user_by_external_identity(
    conn: sqlite3.Connection,
    *,
    provider: str,
    tenant_id: str,
    object_id: str,
) -> dict[str, Any] | None:
    row = conn.execute(
        """
        SELECT u.*, i.identity_id, i.tenant_id, i.object_id
        FROM external_identities i
        JOIN dashboard_users u ON u.user_id=i.user_id
        WHERE i.provider=? AND i.tenant_id=? AND i.object_id=?
        """,
        (provider, tenant_id, object_id),
    ).fetchone()
    return _with_telegram_identity(conn, dict(row) if row else None)


def normalize_telegram_user_id(value: str | int) -> str:
    """Return a canonical numeric Telegram user id or raise ValueError."""
    text = str(value).strip()
    if text.startswith("+"):
        raise ValueError("Telegram user id must be a numeric id")
    if not text.isdigit() or not (5 <= len(text) <= 20):
        raise ValueError("Telegram user id must be a numeric id")
    return str(int(text))


def telegram_user_id_for(conn: sqlite3.Connection, user_id: str) -> str | None:
    row = conn.execute(
        """
        SELECT object_id FROM external_identities
        WHERE provider=? AND user_id=?
        LIMIT 1
        """,
        (TELEGRAM_IDENTITY_PROVIDER, user_id),
    ).fetchone()
    return str(row["object_id"]) if row else None


def _with_telegram_identity(
    conn: sqlite3.Connection, user: dict[str, Any] | None
) -> dict[str, Any] | None:
    if user is None:
        return None
    enriched = dict(user)
    if not enriched.get("telegram_user_id"):
        enriched["telegram_user_id"] = telegram_user_id_for(conn, enriched["user_id"])
    return enriched


def bind_telegram_identity(
    conn: sqlite3.Connection,
    *,
    user_id: str,
    telegram_user_id: str | int,
) -> dict[str, Any]:
    """Link one Telegram user id to a Dashboard user. Replaces a prior binding."""
    user = get_user(conn, user_id)
    if not user:
        raise KeyError(user_id)
    object_id = normalize_telegram_user_id(telegram_user_id)
    existing = find_user_by_external_identity(
        conn,
        provider=TELEGRAM_IDENTITY_PROVIDER,
        tenant_id=TELEGRAM_IDENTITY_TENANT,
        object_id=object_id,
    )
    if existing and existing["user_id"] != user_id:
        raise ValueError("Telegram identity is already linked to another user")

    current_binding = conn.execute(
        """
        SELECT identity_id, object_id FROM external_identities
        WHERE provider=? AND user_id=?
        LIMIT 1
        """,
        (TELEGRAM_IDENTITY_PROVIDER, user_id),
    ).fetchone()
    if current_binding:
        conn.execute(
            """
            UPDATE external_identities
            SET object_id=?, tenant_id=?, updated_at=datetime('now')
            WHERE identity_id=?
            """,
            (object_id, TELEGRAM_IDENTITY_TENANT, current_binding["identity_id"]),
        )
    else:
        try:
            conn.execute(
                """
                INSERT INTO external_identities(
                    identity_id,user_id,provider,tenant_id,object_id,email,display_name
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    str(uuid.uuid4()),
                    user_id,
                    TELEGRAM_IDENTITY_PROVIDER,
                    TELEGRAM_IDENTITY_TENANT,
                    object_id,
                    user.get("email"),
                    user.get("display_name"),
                ),
            )
        except sqlite3.IntegrityError as exc:
            raise ValueError("Telegram identity is already linked to another user") from exc
    return get_user(conn, user_id) or user


def unbind_telegram_identity(conn: sqlite3.Connection, *, user_id: str) -> dict[str, Any]:
    user = get_user(conn, user_id)
    if not user:
        raise KeyError(user_id)
    conn.execute(
        """
        DELETE FROM external_identities
        WHERE provider=? AND user_id=?
        """,
        (TELEGRAM_IDENTITY_PROVIDER, user_id),
    )
    return get_user(conn, user_id) or user


def bind_entra_identity(
    conn: sqlite3.Connection,
    *,
    tenant_id: str,
    object_id: str,
    email: str | None,
    display_name: str | None,
) -> dict[str, Any]:
    """Resolve or JIT-create an Entra user and update mutable profile fields."""
    tenant_id = tenant_id.strip()
    object_id = object_id.strip()
    normalized_email = (email or "").strip() or None
    normalized_name = (display_name or "").strip() or normalized_email or "Dashboard User"

    existing = find_user_by_identity(conn, tenant_id, object_id)
    if existing:
        conn.execute(
            """
            UPDATE dashboard_users
            SET email=?, display_name=?, last_login_at=datetime('now'), updated_at=datetime('now')
            WHERE user_id=?
            """,
            (normalized_email, normalized_name, existing["user_id"]),
        )
        conn.execute(
            """
            UPDATE external_identities
            SET email=?, display_name=?, updated_at=datetime('now')
            WHERE identity_id=?
            """,
            (normalized_email, normalized_name, existing["identity_id"]),
        )
        return get_user(conn, existing["user_id"]) or existing

    primary = bootstrap_primary_admin(conn)
    configured_oid = config_settings.CONFIG.dashboard_auth.initial_admin_oid.strip()
    configured_email = config_settings.CONFIG.dashboard_auth.initial_admin_email.strip().casefold()
    should_bind_primary = bool(configured_oid and object_id == configured_oid)
    if not configured_oid and configured_email and normalized_email:
        should_bind_primary = normalized_email.casefold() == configured_email

    if should_bind_primary:
        user_id = primary["user_id"]
        conflict = conn.execute(
            "SELECT object_id FROM external_identities WHERE provider='entra' AND user_id=?",
            (user_id,),
        ).fetchone()
        if conflict and conflict["object_id"] != object_id:
            raise ValueError("Primary Admin is already bound to another Entra identity")
    else:
        user_id = str(uuid.uuid4())
        conn.execute(
            """
            INSERT INTO dashboard_users(user_id,email,display_name,role,status,account_type)
            VALUES (?, ?, ?, ?, 'active', 'sso')
            """,
            (user_id, normalized_email, normalized_name, ROLE_VIEWER),
        )

    try:
        conn.execute(
            """
            INSERT INTO external_identities(
                identity_id,user_id,provider,tenant_id,object_id,email,display_name
            ) VALUES (?, ?, 'entra', ?, ?, ?, ?)
            """,
            (str(uuid.uuid4()), user_id, tenant_id, object_id, normalized_email, normalized_name),
        )
    except sqlite3.IntegrityError as exc:
        raise ValueError("Entra identity is already linked to another user") from exc

    conn.execute(
        """
        UPDATE dashboard_users
        SET email=?, display_name=?, last_login_at=datetime('now'), updated_at=datetime('now')
        WHERE user_id=?
        """,
        (normalized_email, normalized_name, user_id),
    )
    return get_user(conn, user_id) or {}


def list_users(
    conn: sqlite3.Connection, *, search: str | None = None, limit: int = 200
) -> list[dict[str, Any]]:
    params: list[Any] = []
    where = ""
    if search:
        where = "WHERE email LIKE ? COLLATE NOCASE OR display_name LIKE ? COLLATE NOCASE"
        needle = f"%{search.strip()}%"
        params.extend([needle, needle])
    params.append(max(1, min(limit, 500)))
    rows = conn.execute(
        f"""
        SELECT u.*,
               (SELECT COUNT(*) FROM auth_sessions s
                WHERE s.user_id=u.user_id AND s.revoked_at IS NULL
                  AND s.absolute_expires_at > datetime('now')
                  AND s.idle_expires_at > datetime('now')) AS active_session_count,
               (SELECT i.object_id FROM external_identities i
                WHERE i.user_id=u.user_id AND i.provider='telegram'
                LIMIT 1) AS telegram_user_id
        FROM dashboard_users u {where}
        ORDER BY u.is_primary_admin DESC, u.display_name COLLATE NOCASE
        LIMIT ?
        """,
        params,
    ).fetchall()
    return [dict(row) for row in rows]


def _active_admin_count(conn: sqlite3.Connection) -> int:
    row = conn.execute(
        """
        SELECT COUNT(*) FROM dashboard_users
        WHERE role='Admin' AND status='active'
        """
    ).fetchone()
    return int(row[0] if row else 0)


def update_user_access(
    conn: sqlite3.Connection,
    *,
    user_id: str,
    role: str | None = None,
    status: str | None = None,
) -> dict[str, Any]:
    user = get_user(conn, user_id)
    if not user:
        raise KeyError(user_id)
    if role is not None and role not in VALID_ROLES:
        raise ValueError("Invalid Dashboard role")
    if status is not None and status not in VALID_STATUSES:
        raise ValueError("Invalid Dashboard user status")

    next_role = role or user["role"]
    next_status = status or user["status"]
    if (
        next_status == "active"
        and user["account_type"] == "local"
        and user.get("email")
        and not user.get("email_verified_at")
    ):
        next_status = "pending_verification"
    removes_active_admin = (
        user["role"] == ROLE_ADMIN
        and user["status"] == "active"
        and (next_role != ROLE_ADMIN or next_status != "active")
    )
    if removes_active_admin and _active_admin_count(conn) <= 1:
        raise ValueError("Cannot disable or downgrade the last active Admin")

    conn.execute(
        """
        UPDATE dashboard_users SET role=?, status=?, updated_at=datetime('now')
        WHERE user_id=?
        """,
        (next_role, next_status, user_id),
    )
    if next_status == "disabled":
        conn.execute(
            """
            UPDATE auth_sessions SET revoked_at=datetime('now'), revoke_reason='user_disabled'
            WHERE user_id=? AND revoked_at IS NULL
            """,
            (user_id,),
        )
    return get_user(conn, user_id) or {}


def record_security_event(
    conn: sqlite3.Connection,
    event_type: str,
    *,
    severity: str = "info",
    actor_user_id: str | None = None,
    subject_user_id: str | None = None,
    session_id: str | None = None,
    ip_address: str | None = None,
    user_agent: str | None = None,
    metadata: dict[str, Any] | None = None,
) -> str:
    event_id = str(uuid.uuid4())
    safe_metadata = _sanitize_metadata(metadata or {})
    conn.execute(
        """
        INSERT INTO auth_security_events(
            event_id,event_type,severity,actor_user_id,subject_user_id,session_id,
            ip_address,user_agent,metadata_json
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            event_id, event_type, severity, actor_user_id, subject_user_id,
            session_id, ip_address, user_agent,
            json.dumps(safe_metadata, ensure_ascii=True, separators=(",", ":")),
        ),
    )
    return event_id


def list_security_events(
    conn: sqlite3.Connection, *, limit: int = 200, event_type: str | None = None
) -> list[dict[str, Any]]:
    if event_type:
        rows = conn.execute(
            "SELECT * FROM auth_security_events WHERE event_type=? ORDER BY created_at DESC LIMIT ?",
            (event_type, max(1, min(limit, 500))),
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT * FROM auth_security_events ORDER BY created_at DESC LIMIT ?",
            (max(1, min(limit, 500)),),
        ).fetchall()
    result = []
    for row in rows:
        item = dict(row)
        try:
            item["metadata"] = json.loads(item.pop("metadata_json") or "{}")
        except json.JSONDecodeError:
            item["metadata"] = {}
        result.append(item)
    return result


def purge_auth_records(conn: sqlite3.Connection) -> dict[str, int]:
    """Apply the authentication retention policy without deleting active records."""
    cursor_events = conn.execute(
        "DELETE FROM auth_security_events WHERE created_at < datetime('now','-365 days')"
    )
    cursor_sessions = conn.execute(
        """
        DELETE FROM auth_sessions
        WHERE revoked_at IS NOT NULL AND revoked_at < datetime('now','-90 days')
        """
    )
    cursor_transactions = conn.execute(
        "DELETE FROM auth_oidc_transactions WHERE expires_at < datetime('now')"
    )
    cursor_action_tokens = conn.execute(
        """
        DELETE FROM auth_action_tokens
        WHERE expires_at < datetime('now','-7 days')
           OR (used_at IS NOT NULL AND used_at < datetime('now','-7 days'))
        """
    )
    return {
        "security_events": max(0, cursor_events.rowcount),
        "sessions": max(0, cursor_sessions.rowcount),
        "oidc_transactions": max(0, cursor_transactions.rowcount),
        "action_tokens": max(0, cursor_action_tokens.rowcount),
    }


def _sanitize_metadata(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            str(key): "[redacted]" if str(key).casefold() in _SENSITIVE_METADATA_KEYS
            else _sanitize_metadata(item)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [_sanitize_metadata(item) for item in value]
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    return str(value)
