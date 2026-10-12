"""Database schema helpers for the secure draft workspace."""

from __future__ import annotations

import sqlite3


DRAFT_WORKSPACE_COLUMNS: tuple[tuple[str, str], ...] = (
    ("body_html", "TEXT"),
    ("body_text", "TEXT"),
    ("sender_identity_id", "TEXT"),
    (
        "recipients_json",
        "TEXT NOT NULL DEFAULT '{\"to\":[],\"cc\":[],\"bcc\":[]}'",
    ),
    (
        "delivery_mode",
        "TEXT NOT NULL DEFAULT 'unresolved' "
        "CHECK (delivery_mode IN ('unresolved','new','reply'))",
    ),
    ("reply_to_email_id", "TEXT"),
    ("suggested_reply_to_email_id", "TEXT"),
    ("owner_user_id", "TEXT"),
    ("reviewer_user_id", "TEXT"),
    (
        "review_state",
        "TEXT NOT NULL DEFAULT 'not_requested' CHECK (review_state IN "
        "('not_requested','requested','changes_requested','approved','cancelled','stale'))",
    ),
    ("review_requested_by", "TEXT"),
    ("review_requested_at", "TEXT"),
    ("approval_hash", "TEXT"),
    ("approved_at", "TEXT"),
    ("workspace_revision", "INTEGER NOT NULL DEFAULT 1"),
    ("checkpoint_version", "INTEGER NOT NULL DEFAULT 0"),
    ("send_affecting_hash", "TEXT"),
    ("attachment_hash", "TEXT"),
    (
        "delivery_status",
        "TEXT NOT NULL DEFAULT 'editing' CHECK (delivery_status IN "
        "('editing','scheduled','undoable','sending','sent','failed','cancelled','needs_attention','uncertain'))",
    ),
    ("workspace_updated_at", "TEXT"),
)

PROCESSED_EMAIL_WORKSPACE_COLUMNS: tuple[tuple[str, str], ...] = (
    ("to_recipients_json", "TEXT NOT NULL DEFAULT '[]'"),
    ("cc_recipients_json", "TEXT NOT NULL DEFAULT '[]'"),
    ("bcc_recipients_json", "TEXT NOT NULL DEFAULT '[]'"),
)

WORKSPACE_TABLES_DDL = """
CREATE TABLE IF NOT EXISTS draft_checkpoints (
    checkpoint_id TEXT PRIMARY KEY,
    draft_id TEXT NOT NULL REFERENCES drafts(draft_id) ON DELETE CASCADE,
    version INTEGER NOT NULL,
    revision INTEGER NOT NULL,
    snapshot_json TEXT NOT NULL,
    snapshot_hash TEXT NOT NULL,
    reason TEXT NOT NULL,
    created_by TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE(draft_id, version)
);
CREATE INDEX IF NOT EXISTS idx_draft_checkpoints_draft
    ON draft_checkpoints(draft_id, version DESC);

CREATE TABLE IF NOT EXISTS draft_comments (
    comment_id TEXT PRIMARY KEY,
    draft_id TEXT NOT NULL REFERENCES drafts(draft_id) ON DELETE CASCADE,
    parent_comment_id TEXT REFERENCES draft_comments(comment_id) ON DELETE CASCADE,
    author_user_id TEXT NOT NULL,
    author_display_name TEXT NOT NULL,
    location_type TEXT NOT NULL DEFAULT 'general' CHECK (location_type IN
        ('general','subject','recipient','body','attachment')),
    location_ref TEXT,
    body TEXT NOT NULL,
    resolved_at TEXT,
    resolved_by TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_draft_comments_draft
    ON draft_comments(draft_id, created_at);

CREATE TABLE IF NOT EXISTS draft_edit_leases (
    draft_id TEXT PRIMARY KEY REFERENCES drafts(draft_id) ON DELETE CASCADE,
    holder_user_id TEXT NOT NULL,
    holder_display_name TEXT NOT NULL,
    lease_token TEXT NOT NULL UNIQUE,
    expires_at TEXT NOT NULL,
    updated_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_draft_edit_leases_expiry
    ON draft_edit_leases(expires_at);

CREATE TABLE IF NOT EXISTS draft_ai_proposals (
    proposal_id TEXT PRIMARY KEY,
    draft_id TEXT NOT NULL REFERENCES drafts(draft_id) ON DELETE CASCADE,
    base_revision INTEGER NOT NULL,
    base_hash TEXT NOT NULL,
    action TEXT NOT NULL CHECK (action IN
        ('rewrite','tone','shorten','translate','fact_check')),
    proposal_json TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'open' CHECK (status IN
        ('open','applied','rejected','stale')),
    created_by TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    applied_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_draft_ai_proposals_draft
    ON draft_ai_proposals(draft_id, created_at DESC);

CREATE TABLE IF NOT EXISTS draft_outbox (
    outbox_id TEXT PRIMARY KEY,
    draft_id TEXT NOT NULL REFERENCES drafts(draft_id),
    snapshot_json TEXT NOT NULL,
    snapshot_hash TEXT NOT NULL,
    idempotency_key TEXT NOT NULL UNIQUE,
    status TEXT NOT NULL CHECK (status IN
        ('undoable','scheduled','pending','sending','sent','cancelled','failed','needs_attention','uncertain')),
    scheduled_for TEXT,
    undo_until TEXT,
    claimed_by TEXT,
    claim_expires_at TEXT,
    attempts INTEGER NOT NULL DEFAULT 0,
    last_error TEXT,
    provider_message_id TEXT,
    provider_result_json TEXT,
    created_by TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at TEXT NOT NULL DEFAULT (datetime('now')),
    sent_at TEXT,
    cancelled_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_draft_outbox_due
    ON draft_outbox(status, scheduled_for, undo_until, created_at);
CREATE INDEX IF NOT EXISTS idx_draft_outbox_draft
    ON draft_outbox(draft_id, created_at DESC);

CREATE TABLE IF NOT EXISTS draft_audit_events (
    event_id TEXT PRIMARY KEY,
    draft_id TEXT NOT NULL REFERENCES drafts(draft_id) ON DELETE CASCADE,
    event_type TEXT NOT NULL,
    actor_user_id TEXT NOT NULL,
    metadata_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_draft_audit_events_draft
    ON draft_audit_events(draft_id, created_at DESC);
"""


def ensure_draft_workspace_schema(conn: sqlite3.Connection) -> None:
    """Create workspace tables and add columns to legacy databases idempotently."""
    draft_columns = {row[1] for row in conn.execute("PRAGMA table_info(drafts)")}
    for name, typedef in DRAFT_WORKSPACE_COLUMNS:
        if name not in draft_columns:
            conn.execute(f"ALTER TABLE drafts ADD COLUMN {name} {typedef}")

    processed_columns = {
        row[1] for row in conn.execute("PRAGMA table_info(processed_emails)")
    }
    for name, typedef in PROCESSED_EMAIL_WORKSPACE_COLUMNS:
        if name not in processed_columns:
            conn.execute(f"ALTER TABLE processed_emails ADD COLUMN {name} {typedef}")

    conn.executescript(WORKSPACE_TABLES_DDL)
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_drafts_workspace_state "
        "ON drafts(delivery_status, review_state, workspace_updated_at)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_drafts_workspace_owner "
        "ON drafts(owner_user_id, delivery_status)"
    )
