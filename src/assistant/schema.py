"""Schema helpers for the AI Workspace persistence model."""

from __future__ import annotations

import sqlite3

ASSISTANT_SCHEMA_DDL = """
CREATE TABLE IF NOT EXISTS assistant_threads (
    thread_id TEXT PRIMARY KEY,
    owner_user_id TEXT NOT NULL,
    title TEXT NOT NULL,
    scope_mode TEXT NOT NULL DEFAULT 'portfolio'
        CHECK(scope_mode IN ('portfolio', 'projects')),
    web_enabled INTEGER NOT NULL DEFAULT 0 CHECK(web_enabled IN (0, 1)),
    archived_at TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_assistant_threads_owner_updated
    ON assistant_threads(owner_user_id, archived_at, updated_at DESC, thread_id DESC);

CREATE TABLE IF NOT EXISTS assistant_thread_projects (
    thread_id TEXT NOT NULL REFERENCES assistant_threads(thread_id) ON DELETE CASCADE,
    project_id TEXT NOT NULL REFERENCES projects(project_id) ON DELETE CASCADE,
    position INTEGER NOT NULL DEFAULT 0,
    added_at TEXT NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY(thread_id, project_id)
);
CREATE INDEX IF NOT EXISTS idx_assistant_thread_projects_project
    ON assistant_thread_projects(project_id, thread_id);

CREATE TABLE IF NOT EXISTS assistant_turns (
    turn_id TEXT PRIMARY KEY,
    thread_id TEXT NOT NULL REFERENCES assistant_threads(thread_id) ON DELETE CASCADE,
    client_turn_id TEXT NOT NULL,
    command TEXT,
    user_text TEXT NOT NULL,
    scope_mode_snapshot TEXT NOT NULL CHECK(scope_mode_snapshot IN ('portfolio', 'projects')),
    project_ids_snapshot_json TEXT NOT NULL DEFAULT '[]',
    web_enabled_snapshot INTEGER NOT NULL DEFAULT 0 CHECK(web_enabled_snapshot IN (0, 1)),
    execution_status TEXT NOT NULL DEFAULT 'running'
        CHECK(execution_status IN ('running', 'completed', 'failed')),
    error TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    started_at TEXT NOT NULL DEFAULT (datetime('now')),
    completed_at TEXT,
    UNIQUE(thread_id, client_turn_id)
);
CREATE INDEX IF NOT EXISTS idx_assistant_turns_thread_created
    ON assistant_turns(thread_id, created_at DESC, turn_id DESC);

CREATE TABLE IF NOT EXISTS assistant_messages (
    message_id TEXT PRIMARY KEY,
    thread_id TEXT NOT NULL REFERENCES assistant_threads(thread_id) ON DELETE CASCADE,
    turn_id TEXT REFERENCES assistant_turns(turn_id) ON DELETE SET NULL,
    role TEXT NOT NULL CHECK(role IN ('user', 'assistant', 'system')),
    content TEXT NOT NULL DEFAULT '',
    parts_json TEXT NOT NULL DEFAULT '[]',
    legacy_source TEXT,
    legacy_source_id TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_assistant_messages_thread_created
    ON assistant_messages(thread_id, created_at, message_id);
CREATE UNIQUE INDEX IF NOT EXISTS idx_assistant_messages_legacy_source
    ON assistant_messages(legacy_source, legacy_source_id)
    WHERE legacy_source IS NOT NULL AND legacy_source_id IS NOT NULL;
"""


def ensure_assistant_schema(conn: sqlite3.Connection) -> None:
    """Create all AI Workspace tables and indexes idempotently."""
    conn.executescript(ASSISTANT_SCHEMA_DDL)
