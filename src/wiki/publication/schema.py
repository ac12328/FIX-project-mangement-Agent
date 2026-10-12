"""Additive schema for governed Wiki publication v2."""
from __future__ import annotations

import sqlite3

PUBLICATION_DDL = """
CREATE TABLE IF NOT EXISTS wiki_publications (
 publication_id TEXT PRIMARY KEY,
 fact_id TEXT NOT NULL UNIQUE REFERENCES memory_facts(fact_id),
 fact_version INTEGER NOT NULL,
 decision_id TEXT NOT NULL REFERENCES memory_decisions(decision_id),
 proposal_payload_json TEXT NOT NULL,
 proposed_at TEXT NOT NULL,
 canonical_path TEXT NOT NULL,
 operation TEXT NOT NULL CHECK(operation IN ('upsert','revoke')),
 status TEXT NOT NULL CHECK(status IN ('proposed','publishing','active','failed','manual_review','dead_letter','revoked')),
 planned_content_hash TEXT NOT NULL,
 page_hash TEXT,
 owned_content_hash TEXT,
 page_revision INTEGER NOT NULL DEFAULT 0,
 repair_count INTEGER NOT NULL DEFAULT 0 CHECK(repair_count BETWEEN 0 AND 2),
 attempts INTEGER NOT NULL DEFAULT 0,
 lease_token TEXT,
 lease_expires_at TEXT,
 claimed_at TEXT,
 revoke_phase TEXT CHECK(revoke_phase IS NULL OR revoke_phase IN ('claimed','authority_removed','index_verified','receipt_committed')),
 source_watermark TEXT,
 scope TEXT NOT NULL CHECK(scope IN ('global','project','market','counterparty')),
 visibility TEXT NOT NULL CHECK(visibility IN ('internal_only','external_safe','restricted')),
 project_ids_json TEXT NOT NULL DEFAULT '[]',
 markets_json TEXT NOT NULL DEFAULT '[]',
 counterparties_json TEXT NOT NULL DEFAULT '[]',
 ownership_json TEXT NOT NULL,
 last_error TEXT,
 created_at TEXT NOT NULL DEFAULT(datetime('now')),
 completed_at TEXT,
 revoked_at TEXT
);
CREATE TABLE IF NOT EXISTS wiki_publication_revisions (
 revision_id TEXT PRIMARY KEY,
 publication_id TEXT NOT NULL REFERENCES wiki_publications(publication_id),
 revision INTEGER NOT NULL,
 repair_of_revision INTEGER,
 content_hash TEXT NOT NULL,
 page_hash TEXT NOT NULL,
 content TEXT NOT NULL,
 lint_json TEXT NOT NULL DEFAULT '[]',
 created_at TEXT NOT NULL DEFAULT(datetime('now')),
 UNIQUE(publication_id,revision)
);
CREATE TABLE IF NOT EXISTS wiki_publication_history (
 history_id TEXT PRIMARY KEY,
 publication_id TEXT NOT NULL REFERENCES wiki_publications(publication_id),
 event_type TEXT NOT NULL,
 actor_id TEXT NOT NULL,
 reason TEXT NOT NULL,
 payload_json TEXT NOT NULL DEFAULT '{}',
 created_at TEXT NOT NULL DEFAULT(datetime('now'))
);
CREATE TABLE IF NOT EXISTS wiki_publication_acknowledgements (
 source_watermark TEXT PRIMARY KEY,
 publication_id TEXT NOT NULL UNIQUE REFERENCES wiki_publications(publication_id),
 revision INTEGER NOT NULL,
 content_hash TEXT NOT NULL,
 page_hash TEXT NOT NULL,
 indexed_at TEXT NOT NULL,
 created_at TEXT NOT NULL DEFAULT(datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_wiki_publications_status ON wiki_publications(status,created_at);
CREATE TRIGGER IF NOT EXISTS trg_wiki_publication_revisions_no_update BEFORE UPDATE ON wiki_publication_revisions BEGIN SELECT RAISE(ABORT,'wiki publication revisions are append-only'); END;
CREATE TRIGGER IF NOT EXISTS trg_wiki_publication_revisions_no_delete BEFORE DELETE ON wiki_publication_revisions BEGIN SELECT RAISE(ABORT,'wiki publication revisions are append-only'); END;
CREATE TRIGGER IF NOT EXISTS trg_wiki_publication_history_no_update BEFORE UPDATE ON wiki_publication_history BEGIN SELECT RAISE(ABORT,'wiki publication history is append-only'); END;
CREATE TRIGGER IF NOT EXISTS trg_wiki_publication_history_no_delete BEFORE DELETE ON wiki_publication_history BEGIN SELECT RAISE(ABORT,'wiki publication history is append-only'); END;
CREATE TRIGGER IF NOT EXISTS trg_wiki_publications_immutable_update BEFORE UPDATE OF
 fact_id,fact_version,decision_id,proposal_payload_json,proposed_at,canonical_path,operation,
 planned_content_hash,source_watermark,scope,visibility,project_ids_json,markets_json,
 counterparties_json,ownership_json ON wiki_publications
BEGIN SELECT RAISE(ABORT,'wiki publication proposal is immutable'); END;
CREATE TRIGGER IF NOT EXISTS trg_wiki_publications_no_delete BEFORE DELETE ON wiki_publications BEGIN SELECT RAISE(ABORT,'wiki publication proposal is immutable'); END;
CREATE TRIGGER IF NOT EXISTS trg_wiki_publication_ack_no_update BEFORE UPDATE ON wiki_publication_acknowledgements BEGIN SELECT RAISE(ABORT,'wiki publication acknowledgement is immutable'); END;
CREATE TRIGGER IF NOT EXISTS trg_wiki_publication_ack_no_delete BEFORE DELETE ON wiki_publication_acknowledgements BEGIN SELECT RAISE(ABORT,'wiki publication acknowledgement is immutable'); END;
"""

_REQUIRED_COLUMNS = {
    "attempts": "INTEGER NOT NULL DEFAULT 0",
    "lease_token": "TEXT",
    "lease_expires_at": "TEXT",
    "claimed_at": "TEXT",
    "proposal_payload_json": "TEXT NOT NULL DEFAULT '{}'",
    "proposed_at": "TEXT NOT NULL DEFAULT '1970-01-01T00:00:00+00:00'",
    "revoke_phase": "TEXT",
    "scope": "TEXT NOT NULL DEFAULT 'global'",
    "visibility": "TEXT NOT NULL DEFAULT 'internal_only'",
    "project_ids_json": "TEXT NOT NULL DEFAULT '[]'",
    "markets_json": "TEXT NOT NULL DEFAULT '[]'",
    "counterparties_json": "TEXT NOT NULL DEFAULT '[]'",
}


def ensure_publication_schema(conn: sqlite3.Connection) -> None:
    """Create or repair the v87 schema while preserving existing rows."""
    conn.executescript(PUBLICATION_DDL)
    columns = {row[1] for row in conn.execute("PRAGMA table_info(wiki_publications)")}
    for name, definition in _REQUIRED_COLUMNS.items():
        if name not in columns:
            conn.execute(f"ALTER TABLE wiki_publications ADD COLUMN {name} {definition}")
    ack_columns = {row[1] for row in conn.execute("PRAGMA table_info(wiki_publication_acknowledgements)")}
    for name, definition in (("revision", "INTEGER NOT NULL DEFAULT 0"), ("content_hash", "TEXT NOT NULL DEFAULT ''")):
        if name not in ack_columns:
            conn.execute(f"ALTER TABLE wiki_publication_acknowledgements ADD COLUMN {name} {definition}")
    conn.executescript(PUBLICATION_DDL)
