"""Evidence-backed actual go-live dates, separate from immutable closed project state."""

DDL = """
CREATE TABLE IF NOT EXISTS project_go_live_confirmations (
    project_id TEXT PRIMARY KEY REFERENCES projects(project_id),
    actual_go_live TEXT NOT NULL,
    evidence_id TEXT NOT NULL REFERENCES evidence_store(evidence_id),
    source_quote TEXT NOT NULL,
    source_hash TEXT NOT NULL,
    recorded_by TEXT NOT NULL,
    recorded_at TEXT NOT NULL DEFAULT (datetime('now'))
);
"""
