"""Idempotent PCT closure and linked residual-work receipts."""
DDL="""
CREATE TABLE IF NOT EXISTS pct_completion_receipts (
 project_id TEXT PRIMARY KEY REFERENCES projects(project_id), source_ref TEXT NOT NULL,
 source_hash TEXT NOT NULL, authority TEXT NOT NULL, ticket_ids_json TEXT NOT NULL,
 completed_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS pct_completion_handoffs (
 followup_id TEXT PRIMARY KEY REFERENCES internal_followups(followup_id),
 project_id TEXT NOT NULL REFERENCES projects(project_id), ticket_id TEXT NOT NULL REFERENCES support_tickets(ticket_id)
);
CREATE TRIGGER IF NOT EXISTS pct_receipts_no_update BEFORE UPDATE ON pct_completion_receipts
BEGIN SELECT RAISE(ABORT,'PCT completion receipts are immutable'); END;
CREATE TRIGGER IF NOT EXISTS pct_receipts_no_delete BEFORE DELETE ON pct_completion_receipts
BEGIN SELECT RAISE(ABORT,'PCT completion receipts are immutable'); END;
"""
