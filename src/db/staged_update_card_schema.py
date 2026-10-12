"""Durable, operation-bound confirmation card intent for staged project updates."""

DDL = """
CREATE TABLE IF NOT EXISTS staged_update_card_outbox (
    operation_id TEXT PRIMARY KEY REFERENCES pending_operations(operation_id),
    project_id TEXT NOT NULL REFERENCES projects(project_id),
    chat_id INTEGER NOT NULL,
    bot_fingerprint TEXT NOT NULL,
    body TEXT NOT NULL,
    state TEXT NOT NULL DEFAULT 'pending'
        CHECK(state IN ('pending','sending','sent','uncertain','cancelled')),
    attempts INTEGER NOT NULL DEFAULT 0,
    available_at TEXT NOT NULL,
    lease_token TEXT,
    lease_until TEXT,
    telegram_message_id INTEGER,
    last_error TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_staged_update_card_outbox_due
    ON staged_update_card_outbox(state, available_at);
"""
