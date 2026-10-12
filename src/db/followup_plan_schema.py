"""Durable followup dependency and operation history schema."""
DDL = """
CREATE TABLE IF NOT EXISTS followup_plan_heads (
 project_id TEXT PRIMARY KEY REFERENCES projects(project_id), revision INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS followup_dependencies (
 followup_id TEXT NOT NULL REFERENCES internal_followups(followup_id),
 prerequisite_id TEXT NOT NULL REFERENCES internal_followups(followup_id),
 PRIMARY KEY(followup_id,prerequisite_id), CHECK(followup_id <> prerequisite_id)
);
CREATE TABLE IF NOT EXISTS followup_bundle_receipts (
 project_id TEXT NOT NULL REFERENCES projects(project_id), operation_id TEXT NOT NULL,
 actor_id TEXT NOT NULL, content_hash TEXT NOT NULL, result_json TEXT NOT NULL,
 created_at TEXT NOT NULL, PRIMARY KEY(project_id,operation_id)
);
CREATE TABLE IF NOT EXISTS followup_action_events (
 event_id TEXT PRIMARY KEY, project_id TEXT NOT NULL REFERENCES projects(project_id),
 operation_id TEXT NOT NULL, followup_id TEXT NOT NULL REFERENCES internal_followups(followup_id),
 actor_id TEXT NOT NULL, operation TEXT NOT NULL, before_json TEXT, after_json TEXT NOT NULL,
 reason TEXT NOT NULL, evidence_ref TEXT, created_at TEXT NOT NULL
);
CREATE TRIGGER IF NOT EXISTS followup_events_no_update BEFORE UPDATE ON followup_action_events
BEGIN SELECT RAISE(ABORT,'followup events are immutable'); END;
CREATE TRIGGER IF NOT EXISTS followup_events_no_delete BEFORE DELETE ON followup_action_events
BEGIN SELECT RAISE(ABORT,'followup events are immutable'); END;
CREATE TRIGGER IF NOT EXISTS followup_receipts_no_update BEFORE UPDATE ON followup_bundle_receipts
BEGIN SELECT RAISE(ABORT,'followup receipts are immutable'); END;
CREATE TRIGGER IF NOT EXISTS followup_receipts_no_delete BEFORE DELETE ON followup_bundle_receipts
BEGIN SELECT RAISE(ABORT,'followup receipts are immutable'); END;
CREATE TRIGGER IF NOT EXISTS followup_dependencies_same_project_insert BEFORE INSERT ON followup_dependencies
WHEN (SELECT project_id FROM internal_followups WHERE followup_id=NEW.followup_id)
 IS NOT (SELECT project_id FROM internal_followups WHERE followup_id=NEW.prerequisite_id)
BEGIN SELECT RAISE(ABORT,'cross-project dependency'); END;
CREATE TRIGGER IF NOT EXISTS followup_dependencies_same_project_update BEFORE UPDATE ON followup_dependencies
WHEN (SELECT project_id FROM internal_followups WHERE followup_id=NEW.followup_id)
 IS NOT (SELECT project_id FROM internal_followups WHERE followup_id=NEW.prerequisite_id)
BEGIN SELECT RAISE(ABORT,'cross-project dependency'); END;
"""
