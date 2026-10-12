"""Schema objects for the canonical FIX session registry."""

from __future__ import annotations

import sqlite3


FIX_SESSION_SCHEMA_DDL = """
CREATE TABLE IF NOT EXISTS configuration_sender_policies (
    policy_id TEXT PRIMARY KEY,
    organisation_id TEXT REFERENCES organisations(org_id),
    email_address TEXT NOT NULL COLLATE NOCASE,
    purpose TEXT NOT NULL DEFAULT 'fix_session_configuration'
        CHECK (purpose IN ('fix_session_configuration')),
    require_spf INTEGER NOT NULL DEFAULT 1 CHECK (require_spf IN (0,1)),
    require_dkim INTEGER NOT NULL DEFAULT 1 CHECK (require_dkim IN (0,1)),
    require_dmarc INTEGER NOT NULL DEFAULT 1 CHECK (require_dmarc IN (0,1)),
    require_alignment INTEGER NOT NULL DEFAULT 1 CHECK (require_alignment IN (0,1)),
    auto_confirm_enabled INTEGER NOT NULL DEFAULT 1 CHECK (auto_confirm_enabled IN (0,1)),
    active INTEGER NOT NULL DEFAULT 1 CHECK (active IN (0,1)),
    approved_by TEXT NOT NULL,
    approved_at TEXT NOT NULL,
    revoked_at TEXT,
    notes TEXT,
    UNIQUE(email_address, purpose)
);

CREATE TABLE IF NOT EXISTS fix_session_config_candidates (
    candidate_id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL REFERENCES projects(project_id),
    environment TEXT NOT NULL CHECK (environment IN ('UAT','PRODUCTION')),
    session_key TEXT NOT NULL,
    proposed_snapshot_json TEXT NOT NULL,
    diff_json TEXT NOT NULL DEFAULT '{}',
    source_email_id TEXT,
    source_attachment_id TEXT,
    evidence_id TEXT,
    sender_email TEXT,
    sender_policy_id TEXT REFERENCES configuration_sender_policies(policy_id),
    spf_result TEXT NOT NULL DEFAULT 'unknown' CHECK (spf_result IN ('pass','fail','unknown')),
    dkim_result TEXT NOT NULL DEFAULT 'unknown' CHECK (dkim_result IN ('pass','fail','unknown')),
    dmarc_result TEXT NOT NULL DEFAULT 'unknown' CHECK (dmarc_result IN ('pass','fail','unknown')),
    aligned_sender INTEGER NOT NULL DEFAULT 0 CHECK (aligned_sender IN (0,1)),
    confidence REAL NOT NULL DEFAULT 0 CHECK (confidence >= 0 AND confidence <= 1),
    completeness REAL NOT NULL DEFAULT 0 CHECK (completeness >= 0 AND completeness <= 1),
    changed_fields_json TEXT NOT NULL DEFAULT '[]',
    inherited_fields_json TEXT NOT NULL DEFAULT '[]',
    conflict_reasons_json TEXT NOT NULL DEFAULT '[]',
    field_sources_json TEXT NOT NULL DEFAULT '{}',
    status TEXT NOT NULL DEFAULT 'pending_review'
        CHECK (status IN ('pending_review','confirmed','rejected','superseded')),
    decision_mode TEXT CHECK (decision_mode IN ('auto','operator','backfill') OR decision_mode IS NULL),
    decision_reason TEXT,
    decided_by TEXT,
    received_at TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    decided_at TEXT,
    idempotency_key TEXT UNIQUE
);
CREATE INDEX IF NOT EXISTS idx_fix_candidates_review
    ON fix_session_config_candidates(status, project_id, environment, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_fix_candidates_source_email
    ON fix_session_config_candidates(source_email_id, created_at DESC);

CREATE TABLE IF NOT EXISTS project_fix_sessions (
    session_version_id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL REFERENCES projects(project_id),
    environment TEXT NOT NULL CHECK (environment IN ('UAT','PRODUCTION')),
    session_key TEXT NOT NULL,
    version_no INTEGER NOT NULL CHECK (version_no >= 1),
    is_current INTEGER NOT NULL DEFAULT 1 CHECK (is_current IN (0,1)),
    begin_string TEXT,
    sender_comp_id TEXT NOT NULL,
    target_comp_id TEXT NOT NULL,
    heartbeat_seconds INTEGER CHECK (heartbeat_seconds IS NULL OR heartbeat_seconds > 0),
    reset_seq_num_on_logon INTEGER CHECK (reset_seq_num_on_logon IN (0,1) OR reset_seq_num_on_logon IS NULL),
    account_reference TEXT,
    username_secret_reference TEXT,
    password_secret_reference TEXT,
    metadata_json TEXT NOT NULL DEFAULT '{}',
    source_candidate_id TEXT NOT NULL REFERENCES fix_session_config_candidates(candidate_id),
    confirmation_mode TEXT NOT NULL CHECK (confirmation_mode IN ('auto','operator','backfill')),
    confirmed_by TEXT NOT NULL,
    confirmed_at TEXT NOT NULL,
    superseded_at TEXT,
    UNIQUE(project_id, environment, session_key, version_no)
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_project_fix_sessions_current
    ON project_fix_sessions(project_id, environment, session_key)
    WHERE is_current = 1;
CREATE INDEX IF NOT EXISTS idx_project_fix_sessions_history
    ON project_fix_sessions(project_id, environment, session_key, version_no DESC);

CREATE TABLE IF NOT EXISTS fix_session_endpoints (
    endpoint_id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_version_id TEXT NOT NULL REFERENCES project_fix_sessions(session_version_id) ON DELETE CASCADE,
    route_role TEXT NOT NULL CHECK (route_role IN ('primary','backup','dr')),
    host TEXT NOT NULL,
    port INTEGER NOT NULL CHECK (port BETWEEN 1 AND 65535),
    priority INTEGER NOT NULL DEFAULT 1 CHECK (priority >= 1),
    transport TEXT NOT NULL DEFAULT 'tcp',
    UNIQUE(session_version_id, route_role, priority)
);

CREATE TABLE IF NOT EXISTS fix_session_field_sources (
    source_id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_version_id TEXT NOT NULL REFERENCES project_fix_sessions(session_version_id) ON DELETE CASCADE,
    field_path TEXT NOT NULL,
    source_email_id TEXT,
    source_attachment_id TEXT,
    evidence_id TEXT,
    source_location TEXT,
    extraction_method TEXT NOT NULL DEFAULT 'deterministic',
    inherited_from_version_id TEXT REFERENCES project_fix_sessions(session_version_id),
    value_hash TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE(session_version_id, field_path)
);

CREATE TABLE IF NOT EXISTS fix_environment_resources (
    resource_id TEXT PRIMARY KEY,
    project_id TEXT REFERENCES projects(project_id),
    market TEXT,
    environment TEXT NOT NULL CHECK (environment IN ('UAT','PRODUCTION')),
    resource_type TEXT NOT NULL,
    label TEXT NOT NULL,
    url TEXT,
    value_text TEXT,
    secret_reference TEXT,
    distribution_class TEXT NOT NULL
        CHECK (distribution_class IN ('public','approved_distributable','secret')),
    metadata_json TEXT NOT NULL DEFAULT '{}',
    source_evidence_id TEXT,
    effective_from TEXT,
    effective_to TEXT,
    is_current INTEGER NOT NULL DEFAULT 1 CHECK (is_current IN (0,1)),
    approved_by TEXT NOT NULL,
    approved_at TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_fix_environment_resources_lookup
    ON fix_environment_resources(project_id, market, environment, resource_type, is_current);
"""


def ensure_fix_session_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(FIX_SESSION_SCHEMA_DDL)
