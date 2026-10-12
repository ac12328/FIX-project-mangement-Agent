"""Durable reply-linked commitment card delivery intents."""

DDL = """
CREATE TABLE IF NOT EXISTS email_commitment_card_outbox (
    reply_id TEXT PRIMARY KEY REFERENCES inbound_replies(reply_id),
    project_id TEXT NOT NULL REFERENCES projects(project_id),
    commitment_text TEXT NOT NULL,
    due_date TEXT NOT NULL,
    chat_id INTEGER,
    bot_fingerprint TEXT,
    state TEXT NOT NULL DEFAULT 'pending'
        CHECK(state IN ('pending','sending','sent','uncertain','cancelled')),
    attempts INTEGER NOT NULL DEFAULT 0,
    available_at TEXT NOT NULL,
    lease_token TEXT,
    lease_until TEXT,
    telegram_message_id TEXT,
    last_error TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_email_commitment_card_pending
    ON email_commitment_card_outbox(state, available_at);
"""
