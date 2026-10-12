"""Durable source/route ledger for inbound mail; no message body is stored."""

BASE_DDL = """
CREATE TABLE IF NOT EXISTS inbound_email_work (
    email_id TEXT PRIMARY KEY REFERENCES processed_emails(email_id),
    source_type TEXT NOT NULL CHECK (source_type IN ('imap','graph','legacy')),
    account_key TEXT,
    mailbox TEXT,
    uid_validity INTEGER,
    source_uid INTEGER,
    classification_json TEXT NOT NULL DEFAULT '{}',
    checkpoint_json TEXT NOT NULL DEFAULT '{}',
    attempt_count INTEGER NOT NULL DEFAULT 0,
    source_retry_count INTEGER NOT NULL DEFAULT 0 CHECK (source_retry_count >= 0),
    route_state TEXT NOT NULL CHECK (route_state IN
        ('pending','routing','completed','needs_reconciliation')),
    lease_token TEXT,
    lease_expires_at TEXT,
    last_error TEXT,
    captured_at TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at TEXT NOT NULL DEFAULT (datetime('now')),
    CHECK (source_type != 'imap' OR
        (account_key IS NOT NULL AND mailbox IS NOT NULL AND
         uid_validity > 0 AND source_uid > 0)),
    CHECK ((route_state = 'routing') =
        (lease_token IS NOT NULL AND lease_expires_at IS NOT NULL))
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_inbound_work_imap_source
    ON inbound_email_work(account_key, mailbox, uid_validity, source_uid)
    WHERE source_type = 'imap';
CREATE INDEX IF NOT EXISTS idx_inbound_work_route_state
    ON inbound_email_work(route_state, lease_expires_at, captured_at);
"""

# Added after v123; the historical reconciliation must not index a v135 table
# until v136 has added source_retry_count.
RETRY_INDEX_DDL = """CREATE INDEX IF NOT EXISTS idx_inbound_work_imap_pending_retry
    ON inbound_email_work(source_type, account_key, mailbox, route_state,
                          source_retry_count, captured_at, email_id);
"""
DDL = BASE_DDL + RETRY_INDEX_DDL
