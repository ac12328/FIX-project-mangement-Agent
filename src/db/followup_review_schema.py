"""Durable email action-plan proposals, separate from project field reviews."""
DDL = """
CREATE TABLE IF NOT EXISTS followup_plan_reviews (
 review_id TEXT PRIMARY KEY, project_id TEXT NOT NULL REFERENCES projects(project_id),
 source_ref TEXT NOT NULL, source_hash TEXT, source_json TEXT NOT NULL,
 snapshot_json TEXT NOT NULL, changes_json TEXT NOT NULL, review_reason TEXT NOT NULL,
 status TEXT NOT NULL DEFAULT 'pending' CHECK(status IN ('pending','approved','rejected')),
 revision INTEGER NOT NULL DEFAULT 0, created_at TEXT NOT NULL, updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_followup_plan_reviews_pending ON followup_plan_reviews(project_id,status,created_at);
CREATE TABLE IF NOT EXISTS followup_plan_review_decisions (
 decision_id TEXT PRIMARY KEY, review_id TEXT NOT NULL UNIQUE REFERENCES followup_plan_reviews(review_id),
 actor_id TEXT NOT NULL, decision TEXT NOT NULL CHECK(decision IN ('approved','rejected')),
 request_hash TEXT NOT NULL, reason TEXT NOT NULL, bundle_operation_id TEXT,
 result_json TEXT NOT NULL, created_at TEXT NOT NULL
);
CREATE TRIGGER IF NOT EXISTS followup_plan_reviews_source_immutable
BEFORE UPDATE OF project_id,source_ref,source_hash,source_json,snapshot_json,changes_json,review_reason,created_at ON followup_plan_reviews
BEGIN SELECT RAISE(ABORT,'action review proposal is immutable'); END;
CREATE TRIGGER IF NOT EXISTS followup_plan_reviews_resolution_once
BEFORE UPDATE OF status,revision ON followup_plan_reviews
WHEN OLD.status != 'pending' OR NEW.status NOT IN ('approved','rejected') OR NEW.revision != OLD.revision + 1
BEGIN SELECT RAISE(ABORT,'action review resolves once'); END;
CREATE TRIGGER IF NOT EXISTS followup_plan_reviews_no_delete BEFORE DELETE ON followup_plan_reviews
BEGIN SELECT RAISE(ABORT,'action reviews are retained'); END;
CREATE TRIGGER IF NOT EXISTS followup_plan_review_decisions_no_update BEFORE UPDATE ON followup_plan_review_decisions
BEGIN SELECT RAISE(ABORT,'action review decisions are immutable'); END;
CREATE TRIGGER IF NOT EXISTS followup_plan_review_decisions_no_delete BEFORE DELETE ON followup_plan_review_decisions
BEGIN SELECT RAISE(ABORT,'action review decisions are immutable'); END;
"""
