"""Future inbound project advisory intents, independent of information cards."""

DDL = """
CREATE TABLE IF NOT EXISTS project_advisory_outbox (
    email_id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL REFERENCES projects(project_id),
    subject TEXT NOT NULL,
    event_summary TEXT NOT NULL,
    chat_id INTEGER,
    bot_fingerprint TEXT,
    state TEXT NOT NULL CHECK(state IN ('pending','sending','sent','uncertain','blocked','skipped')),
    advisory_json TEXT,
    semantic_key TEXT,
    snapshot_json TEXT,
    evaluated_at TEXT,
    superseded_at TEXT,
    delivery_group TEXT,
    delivery_mode TEXT NOT NULL DEFAULT 'standalone'
        CHECK(delivery_mode IN ('standalone','presentation')),
    attempts INTEGER NOT NULL DEFAULT 0,
    available_at TEXT NOT NULL,
    lease_token TEXT,
    lease_until TEXT,
    telegram_message_id INTEGER,
    last_error TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    CHECK ((chat_id IS NULL) = (bot_fingerprint IS NULL))
);
CREATE INDEX IF NOT EXISTS idx_project_advisory_due
    ON project_advisory_outbox(state, available_at);
"""


def ensure_advisory_delivery_columns(conn) -> None:
    """Reconcile future-only delivery metadata without replaying historical rows."""
    columns = {row[1] for row in conn.execute("PRAGMA table_info(project_advisory_outbox)")}
    for name, definition in (
        ("semantic_key", "TEXT"), ("snapshot_json", "TEXT"),
        ("evaluated_at", "TEXT"), ("superseded_at", "TEXT"), ("delivery_group", "TEXT"),
        ("delivery_mode", "TEXT NOT NULL DEFAULT 'standalone' CHECK(delivery_mode IN ('standalone','presentation'))"),
    ):
        if name not in columns:
            conn.execute(f"ALTER TABLE project_advisory_outbox ADD COLUMN {name} {definition}")
    conn.execute("""CREATE INDEX IF NOT EXISTS idx_project_advisory_semantics
        ON project_advisory_outbox(project_id,chat_id,bot_fingerprint,evaluated_at)
        WHERE semantic_key IS NOT NULL""")

    from src.db.inbound_presentation_schema import ensure_presentation_delivery_columns
    ensure_presentation_delivery_columns(conn)
