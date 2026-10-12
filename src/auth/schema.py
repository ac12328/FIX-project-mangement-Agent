"""Schema bootstrap for Dashboard identity, session, and security data."""

from __future__ import annotations

import sqlite3


def _dashboard_users_ddl(
    table_name: str = "dashboard_users", *, if_not_exists: bool = False
) -> str:
    clause = "IF NOT EXISTS " if if_not_exists else ""
    return f"""
CREATE TABLE {clause}{table_name} (
    user_id TEXT PRIMARY KEY,
    email TEXT,
    display_name TEXT NOT NULL,
    role TEXT NOT NULL DEFAULT 'Viewer' CHECK (role IN ('Admin','Editor','Viewer')),
    status TEXT NOT NULL DEFAULT 'active'
        CHECK (status IN ('active','disabled','pending_verification')),
    account_type TEXT NOT NULL DEFAULT 'sso' CHECK (account_type IN ('sso','local')),
    is_primary_admin INTEGER NOT NULL DEFAULT 0 CHECK (is_primary_admin IN (0,1)),
    email_verified_at TEXT,
    last_login_at TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at TEXT NOT NULL DEFAULT (datetime('now'))
)
"""


def _auth_sessions_ddl(
    table_name: str = "auth_sessions", *, if_not_exists: bool = False
) -> str:
    clause = "IF NOT EXISTS " if if_not_exists else ""
    return f"""
CREATE TABLE {clause}{table_name} (
    session_id TEXT PRIMARY KEY,
    user_id TEXT NOT NULL REFERENCES dashboard_users(user_id) ON DELETE CASCADE,
    token_hash TEXT NOT NULL UNIQUE,
    csrf_hash TEXT NOT NULL,
    auth_method TEXT NOT NULL
        CHECK (auth_method IN ('entra','emergency','legacy','local','disabled')),
    ip_address TEXT,
    user_agent TEXT,
    created_at TEXT NOT NULL,
    last_seen_at TEXT NOT NULL,
    idle_expires_at TEXT NOT NULL,
    absolute_expires_at TEXT NOT NULL,
    revoked_at TEXT,
    revoke_reason TEXT
)
"""


AUTH_SCHEMA_DDL = f"""
{_dashboard_users_ddl(if_not_exists=True)};
CREATE INDEX IF NOT EXISTS idx_dashboard_users_email ON dashboard_users(email COLLATE NOCASE);
CREATE INDEX IF NOT EXISTS idx_dashboard_users_role_status ON dashboard_users(role, status, account_type);

CREATE TABLE IF NOT EXISTS external_identities (
    identity_id TEXT PRIMARY KEY,
    user_id TEXT NOT NULL REFERENCES dashboard_users(user_id) ON DELETE CASCADE,
    provider TEXT NOT NULL DEFAULT 'entra',
    tenant_id TEXT NOT NULL,
    object_id TEXT NOT NULL,
    email TEXT,
    display_name TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE(provider, tenant_id, object_id)
);
CREATE INDEX IF NOT EXISTS idx_external_identities_user ON external_identities(user_id);
CREATE INDEX IF NOT EXISTS idx_external_identities_provider_object
    ON external_identities(provider, object_id);

{_auth_sessions_ddl(if_not_exists=True)};
CREATE INDEX IF NOT EXISTS idx_auth_sessions_user_active
    ON auth_sessions(user_id, revoked_at, absolute_expires_at);
CREATE INDEX IF NOT EXISTS idx_auth_sessions_retention ON auth_sessions(revoked_at, absolute_expires_at);

CREATE TABLE IF NOT EXISTS auth_oidc_transactions (
    transaction_id TEXT PRIMARY KEY,
    state_hash TEXT NOT NULL UNIQUE,
    nonce_hash TEXT NOT NULL,
    pkce_verifier_hash TEXT NOT NULL,
    redirect_to TEXT NOT NULL DEFAULT '/',
    ip_address TEXT,
    created_at TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    used_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_auth_oidc_transactions_expiry ON auth_oidc_transactions(expires_at, used_at);

CREATE TABLE IF NOT EXISTS local_auth_credentials (
    user_id TEXT PRIMARY KEY REFERENCES dashboard_users(user_id) ON DELETE CASCADE,
    username TEXT NOT NULL UNIQUE COLLATE NOCASE,
    password_hash TEXT NOT NULL,
    totp_ciphertext TEXT NOT NULL,
    totp_nonce TEXT NOT NULL,
    totp_key_version TEXT NOT NULL DEFAULT 'v1',
    last_totp_step INTEGER,
    enabled INTEGER NOT NULL DEFAULT 1 CHECK (enabled IN (0,1)),
    password_changed_at TEXT NOT NULL DEFAULT (datetime('now')),
    totp_changed_at TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS local_recovery_codes (
    recovery_code_id TEXT PRIMARY KEY,
    user_id TEXT NOT NULL REFERENCES dashboard_users(user_id) ON DELETE CASCADE,
    code_hash TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    used_at TEXT,
    UNIQUE(user_id, code_hash)
);
CREATE INDEX IF NOT EXISTS idx_local_recovery_codes_user ON local_recovery_codes(user_id, used_at);

CREATE TABLE IF NOT EXISTS auth_action_tokens (
    token_id TEXT PRIMARY KEY,
    user_id TEXT NOT NULL REFERENCES dashboard_users(user_id) ON DELETE CASCADE,
    action_type TEXT NOT NULL CHECK (action_type IN ('email_verification','password_reset')),
    token_hash TEXT NOT NULL UNIQUE,
    created_at TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    used_at TEXT,
    requested_ip TEXT
);
CREATE INDEX IF NOT EXISTS idx_auth_action_tokens_user
    ON auth_action_tokens(user_id, action_type, used_at, expires_at);
CREATE INDEX IF NOT EXISTS idx_auth_action_tokens_expiry
    ON auth_action_tokens(expires_at, used_at);

CREATE TABLE IF NOT EXISTS auth_security_events (
    event_id TEXT PRIMARY KEY,
    event_type TEXT NOT NULL,
    severity TEXT NOT NULL DEFAULT 'info' CHECK (severity IN ('info','warning','critical')),
    actor_user_id TEXT REFERENCES dashboard_users(user_id) ON DELETE SET NULL,
    subject_user_id TEXT REFERENCES dashboard_users(user_id) ON DELETE SET NULL,
    session_id TEXT,
    ip_address TEXT,
    user_agent TEXT,
    metadata_json TEXT NOT NULL DEFAULT '{{}}',
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_auth_security_events_created ON auth_security_events(created_at DESC);
CREATE INDEX IF NOT EXISTS idx_auth_security_events_type ON auth_security_events(event_type, created_at DESC);

CREATE TABLE IF NOT EXISTS auth_rate_limits (
    bucket_type TEXT NOT NULL CHECK (bucket_type IN ('ip','account','global')),
    bucket_key TEXT NOT NULL,
    window_started_at TEXT NOT NULL,
    failure_count INTEGER NOT NULL DEFAULT 0 CHECK (failure_count >= 0),
    blocked_until TEXT,
    updated_at TEXT NOT NULL,
    PRIMARY KEY(bucket_type, bucket_key)
);
"""


def ensure_auth_schema(conn: sqlite3.Connection) -> None:
    """Create or reconcile all current Dashboard authentication tables and indexes."""
    _reconcile_v96_tables(conn)
    conn.executescript(AUTH_SCHEMA_DDL)
    violations: list[sqlite3.Row | tuple] = []
    for table_name in (
        "external_identities",
        "auth_sessions",
        "local_auth_credentials",
        "local_recovery_codes",
        "auth_action_tokens",
        "auth_security_events",
    ):
        violations.extend(conn.execute(f"PRAGMA foreign_key_check({table_name})").fetchall())
    if violations:
        raise sqlite3.IntegrityError(f"Dashboard auth foreign key check failed: {violations[:3]}")


def _reconcile_v96_tables(conn: sqlite3.Connection) -> None:
    user_sql = _table_sql(conn, "dashboard_users")
    session_sql = _table_sql(conn, "auth_sessions")
    rebuild_users = bool(
        user_sql
        and (
            "email_verified_at" not in _table_columns(conn, "dashboard_users")
            or "pending_verification" not in user_sql
        )
    )
    rebuild_sessions = bool(session_sql and "'local'" not in session_sql)
    if not rebuild_users and not rebuild_sessions:
        return

    if conn.in_transaction:
        conn.commit()
    foreign_keys_enabled = bool(conn.execute("PRAGMA foreign_keys").fetchone()[0])
    conn.execute("PRAGMA foreign_keys=OFF")
    try:
        if rebuild_users:
            _rebuild_dashboard_users(conn)
        if rebuild_sessions:
            _rebuild_auth_sessions(conn)
        conn.commit()
    finally:
        conn.execute(f"PRAGMA foreign_keys={'ON' if foreign_keys_enabled else 'OFF'}")


def _rebuild_dashboard_users(conn: sqlite3.Connection) -> None:
    conn.execute("DROP TABLE IF EXISTS dashboard_users_v96")
    conn.execute(_dashboard_users_ddl("dashboard_users_v96"))
    columns = _table_columns(conn, "dashboard_users")
    verified_expression = (
        "email_verified_at"
        if "email_verified_at" in columns
        else "CASE WHEN account_type='local' AND status='active' AND email IS NOT NULL "
             "THEN COALESCE(last_login_at, created_at) ELSE NULL END"
    )
    conn.execute(
        f"""
        INSERT INTO dashboard_users_v96(
            user_id,email,display_name,role,status,account_type,is_primary_admin,
            email_verified_at,last_login_at,created_at,updated_at
        )
        SELECT user_id,email,display_name,role,status,account_type,is_primary_admin,
               {verified_expression},last_login_at,created_at,updated_at
        FROM dashboard_users
        """
    )
    conn.execute("DROP TABLE dashboard_users")
    conn.execute("ALTER TABLE dashboard_users_v96 RENAME TO dashboard_users")


def _rebuild_auth_sessions(conn: sqlite3.Connection) -> None:
    conn.execute("DROP TABLE IF EXISTS auth_sessions_v96")
    conn.execute(_auth_sessions_ddl("auth_sessions_v96"))
    conn.execute(
        """
        INSERT INTO auth_sessions_v96(
            session_id,user_id,token_hash,csrf_hash,auth_method,ip_address,user_agent,
            created_at,last_seen_at,idle_expires_at,absolute_expires_at,revoked_at,revoke_reason
        )
        SELECT session_id,user_id,token_hash,csrf_hash,auth_method,ip_address,user_agent,
               created_at,last_seen_at,idle_expires_at,absolute_expires_at,revoked_at,revoke_reason
        FROM auth_sessions
        """
    )
    conn.execute("DROP TABLE auth_sessions")
    conn.execute("ALTER TABLE auth_sessions_v96 RENAME TO auth_sessions")


def _table_sql(conn: sqlite3.Connection, table_name: str) -> str:
    row = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name=?",
        (table_name,),
    ).fetchone()
    return str(row[0] or "") if row else ""


def _table_columns(conn: sqlite3.Connection, table_name: str) -> set[str]:
    return {str(row[1]) for row in conn.execute(f"PRAGMA table_info({table_name})")}
