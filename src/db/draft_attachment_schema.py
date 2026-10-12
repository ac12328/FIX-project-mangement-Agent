"""Committed decisions for journaled draft attachment filesystem mutations."""

DDL = """
CREATE TABLE IF NOT EXISTS draft_attachment_operations (
    operation_id TEXT PRIMARY KEY,
    draft_id TEXT NOT NULL REFERENCES drafts(draft_id),
    kind TEXT NOT NULL CHECK(kind IN ('upload', 'delete')),
    filename TEXT NOT NULL,
    size INTEGER NOT NULL CHECK(size >= 0),
    sha256 TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_draft_attachment_operations_draft
    ON draft_attachment_operations(draft_id);
"""
