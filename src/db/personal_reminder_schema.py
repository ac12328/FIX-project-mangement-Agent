"""Immutable intent and reviewable outcome for personal Telegram reminders."""

DDL = """
CREATE TABLE IF NOT EXISTS personal_reminder_deliveries (
    run_id TEXT PRIMARY KEY REFERENCES assistant_schedule_runs(run_id),
    schedule_id TEXT NOT NULL REFERENCES assistant_schedules(schedule_id),
    chat_id INTEGER NOT NULL,
    bot_fingerprint TEXT NOT NULL,
    text TEXT NOT NULL,
    state TEXT NOT NULL CHECK (state IN ('sending','sent','uncertain','not_sent')),
    message_ids_json TEXT NOT NULL DEFAULT '[]',
    last_error TEXT,
    review_version INTEGER NOT NULL DEFAULT 0,
    reviewer TEXT,
    evidence_ref TEXT,
    linked_item_review_outcome TEXT CHECK (
        linked_item_review_outcome IN ('resolved','acknowledged_manual_followup')
    ),
    linked_item_reviewer TEXT,
    linked_item_evidence_ref TEXT,
    linked_item_review_note TEXT,
    linked_item_reviewed_at TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_personal_reminder_deliveries_review
    ON personal_reminder_deliveries(state, created_at);
"""
