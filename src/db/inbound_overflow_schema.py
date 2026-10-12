"""Durable IMAP overflow card batches; in-flight sends are never auto-replayed."""

DDL = """
CREATE TABLE IF NOT EXISTS inbound_overflow_batches (
    batch_id TEXT PRIMARY KEY,
    chat_id INTEGER NOT NULL,
    text TEXT NOT NULL,
    content_sha256 TEXT NOT NULL,
    state TEXT NOT NULL CHECK(state IN ('sending','locally_recorded')),
    message_ids_json TEXT,
    review_version INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE TABLE IF NOT EXISTS inbound_overflow_items (
    email_id TEXT PRIMARY KEY REFERENCES inbound_email_work(email_id),
    chat_id INTEGER NOT NULL,
    summary TEXT NOT NULL,
    batch_id TEXT REFERENCES inbound_overflow_batches(batch_id),
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_inbound_overflow_pending
    ON inbound_overflow_items(chat_id, created_at, email_id)
    WHERE batch_id IS NULL;
CREATE INDEX IF NOT EXISTS idx_inbound_overflow_batch
    ON inbound_overflow_items(batch_id);
CREATE TABLE IF NOT EXISTS inbound_overflow_resolutions (
    batch_id TEXT PRIMARY KEY REFERENCES inbound_overflow_batches(batch_id),
    decision TEXT NOT NULL CHECK(decision='confirmed_sent'),
    message_ids_json TEXT NOT NULL,
    evidence_ref TEXT NOT NULL,
    actor TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
"""
