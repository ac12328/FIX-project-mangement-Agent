"""Committed delivery intents for contact identity suggestion cards."""

DDL = """
CREATE TABLE IF NOT EXISTS contact_identity_card_outbox (
    suggestion_id TEXT PRIMARY KEY REFERENCES contact_suggestions(suggestion_id),
    source_email_id TEXT REFERENCES processed_emails(email_id),
    project_id TEXT REFERENCES projects(project_id),
    chat_id INTEGER,
    bot_fingerprint TEXT,
    card_text TEXT NOT NULL,
    state TEXT NOT NULL DEFAULT 'pending'
        CHECK(state IN ('pending','sending','sent','uncertain','blocked','skipped')),
    attempts INTEGER NOT NULL DEFAULT 0,
    lease_token TEXT,
    lease_until TEXT,
    telegram_message_id INTEGER,
    last_error TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at TEXT NOT NULL DEFAULT (datetime('now')),
    CHECK ((chat_id IS NOT NULL AND bot_fingerprint IS NOT NULL)
           OR (state = 'blocked' AND chat_id IS NULL AND bot_fingerprint IS NULL))
);
CREATE INDEX IF NOT EXISTS idx_contact_identity_card_outbox_due
    ON contact_identity_card_outbox(state, created_at);
"""
