"""Durable draft-card intent, created with future drafts only."""

DDL = """
CREATE TABLE IF NOT EXISTS draft_card_outbox (
    draft_id TEXT PRIMARY KEY REFERENCES drafts(draft_id),
    to_email TEXT NOT NULL,
    subject TEXT NOT NULL,
    chat_id INTEGER,
    bot_fingerprint TEXT,
    state TEXT NOT NULL DEFAULT 'pending'
        CHECK(state IN ('pending','sending','sent','uncertain','cancelled')),
    attachment_state TEXT NOT NULL DEFAULT 'ready'
        CHECK(attachment_state IN ('ready','preparing','needs_review')),
    attempts INTEGER NOT NULL DEFAULT 0,
    available_at TEXT NOT NULL,
    lease_token TEXT,
    lease_until TEXT,
    telegram_message_ids_json TEXT,
    last_error TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_draft_card_outbox_due
    ON draft_card_outbox(state, available_at);
"""
