"""Durable, independently reconcilable project-update presentation intents."""

DDL = """
CREATE TABLE IF NOT EXISTS inbound_presentation_outbox (
    email_id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL REFERENCES projects(project_id),
    kind TEXT NOT NULL CHECK(kind IN ('info_card','plain')),
    payload_json TEXT NOT NULL,
    delivery_payload_json TEXT,
    chat_id INTEGER,
    bot_fingerprint TEXT,
    state TEXT NOT NULL CHECK(state IN ('pending','sending','sent','uncertain','blocked')),
    attempts INTEGER NOT NULL DEFAULT 0,
    available_at TEXT NOT NULL,
    lease_token TEXT,
    lease_until TEXT,
    telegram_message_id TEXT,
    last_error TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    CHECK ((chat_id IS NULL) = (bot_fingerprint IS NULL))
);
CREATE INDEX IF NOT EXISTS idx_inbound_presentation_due
    ON inbound_presentation_outbox(state, available_at);
"""


def ensure_presentation_delivery_columns(conn) -> None:
    """Freeze actual combined content separately from immutable source identity."""
    columns = {row[1] for row in conn.execute("PRAGMA table_info(inbound_presentation_outbox)")}
    if "delivery_payload_json" not in columns:
        conn.execute("ALTER TABLE inbound_presentation_outbox ADD COLUMN delivery_payload_json TEXT")
