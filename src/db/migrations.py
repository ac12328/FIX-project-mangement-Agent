"""
Schema versioning and migrations for AgentFIXBPRO.
"""

import logging
import re
import sqlite3

from config.defaults import CURRENT_SCHEMA_VERSION

from src.db.migration_dispatch import MigrationStep, apply_migration_steps

logger = logging.getLogger(__name__)

_MEMORY_CANDIDATES_DDL = """
CREATE TABLE IF NOT EXISTS memory_candidates (
    candidate_id TEXT NOT NULL,
    observation_id TEXT NOT NULL REFERENCES learning_observations(observation_id),
    candidate_version INTEGER NOT NULL CHECK (candidate_version >= 1),
    target_type TEXT NOT NULL CHECK (target_type IN ('semantic_fact','preference','procedure','wiki_publication')),
    subject_type TEXT NOT NULL,
    subject_id TEXT,
    predicate TEXT NOT NULL,
    proposed_value_json TEXT NOT NULL,
    scope_type TEXT NOT NULL CHECK (scope_type IN ('contact','project','counterparty','operator','market','organization','global')),
    scope_id TEXT,
    confidence REAL NOT NULL CHECK (confidence >= 0 AND confidence <= 1),
    suggested_expiry TEXT,
    reasoning_summary TEXT NOT NULL,
    risk_signals_json TEXT NOT NULL DEFAULT '[]',
    evidence_refs_json TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'proposed' CHECK (status = 'proposed'),
    extractor_version TEXT NOT NULL,
    model_version TEXT NOT NULL,
    prompt_version TEXT NOT NULL,
    schema_version TEXT NOT NULL,
    revision_of_validation_id TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY(candidate_id, candidate_version),
    UNIQUE(candidate_id, candidate_version)
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_memory_candidates_observation_extractor
    ON memory_candidates(observation_id, target_type, extractor_version, candidate_version);
CREATE INDEX IF NOT EXISTS idx_memory_candidates_scope
    ON memory_candidates(scope_type, scope_id, target_type, created_at DESC);
"""

_CANDIDATE_GOVERNANCE_DDL = """
CREATE TRIGGER IF NOT EXISTS trg_memory_candidates_content_immutable
BEFORE UPDATE ON memory_candidates
WHEN NEW.candidate_id IS NOT OLD.candidate_id
  OR NEW.observation_id IS NOT OLD.observation_id
  OR NEW.candidate_version IS NOT OLD.candidate_version
  OR NEW.target_type IS NOT OLD.target_type
  OR NEW.subject_type IS NOT OLD.subject_type
  OR NEW.subject_id IS NOT OLD.subject_id
  OR NEW.predicate IS NOT OLD.predicate
  OR NEW.proposed_value_json IS NOT OLD.proposed_value_json
  OR NEW.scope_type IS NOT OLD.scope_type
  OR NEW.scope_id IS NOT OLD.scope_id
  OR NEW.confidence IS NOT OLD.confidence
  OR NEW.suggested_expiry IS NOT OLD.suggested_expiry
  OR NEW.reasoning_summary IS NOT OLD.reasoning_summary
  OR NEW.risk_signals_json IS NOT OLD.risk_signals_json
  OR NEW.evidence_refs_json IS NOT OLD.evidence_refs_json
  OR NEW.extractor_version IS NOT OLD.extractor_version
  OR NEW.model_version IS NOT OLD.model_version
  OR NEW.prompt_version IS NOT OLD.prompt_version
  OR NEW.schema_version IS NOT OLD.schema_version
  OR NEW.revision_of_validation_id IS NOT OLD.revision_of_validation_id
  OR NEW.created_at IS NOT OLD.created_at
BEGIN
    SELECT RAISE(ABORT, 'memory candidate content is immutable');
END;
CREATE TABLE IF NOT EXISTS candidate_validation_runs (
    validation_id TEXT PRIMARY KEY,
    candidate_id TEXT NOT NULL,
    candidate_version INTEGER NOT NULL,
    validator_version TEXT NOT NULL,
    policy_version TEXT NOT NULL,
    policy_json TEXT NOT NULL,
    passed INTEGER NOT NULL CHECK (passed IN (0,1)),
    findings_json TEXT NOT NULL,
    risk_level TEXT NOT NULL CHECK (risk_level IN ('low','medium','high','critical')),
    risk_score INTEGER NOT NULL CHECK (risk_score >= 0 AND risk_score <= 100),
    input_hash TEXT NOT NULL,
    input_snapshot_json TEXT NOT NULL,
    findings_hash TEXT NOT NULL,
    disposition TEXT NOT NULL CHECK (disposition IN (
        'needs_revision','needs_review','rejected','quarantined'
    )),
    requires_review INTEGER NOT NULL CHECK (requires_review IN (0,1)),
    requires_evaluation INTEGER NOT NULL CHECK (requires_evaluation IN (0,1)),
    validation_at TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    FOREIGN KEY(candidate_id, candidate_version)
        REFERENCES memory_candidates(candidate_id, candidate_version)
);
CREATE TABLE IF NOT EXISTS candidate_governance_states (
    candidate_id TEXT NOT NULL,
    candidate_version INTEGER NOT NULL,
    disposition TEXT NOT NULL CHECK (disposition IN (
        'needs_revision','needs_review','rejected','quarantined'
    )),
    validation_id TEXT NOT NULL REFERENCES candidate_validation_runs(validation_id),
    updated_at TEXT NOT NULL,
    PRIMARY KEY(candidate_id, candidate_version),
    FOREIGN KEY(candidate_id, candidate_version)
        REFERENCES memory_candidates(candidate_id, candidate_version)
);
CREATE INDEX IF NOT EXISTS idx_candidate_governance_disposition
    ON candidate_governance_states(disposition, updated_at DESC);
CREATE INDEX IF NOT EXISTS idx_candidate_validation_candidate
    ON candidate_validation_runs(candidate_id, candidate_version, created_at DESC);
CREATE TRIGGER IF NOT EXISTS trg_candidate_validation_no_update
BEFORE UPDATE ON candidate_validation_runs
BEGIN
    SELECT RAISE(ABORT, 'candidate_validation_runs is append-only');
END;
CREATE TRIGGER IF NOT EXISTS trg_candidate_validation_no_delete
BEFORE DELETE ON candidate_validation_runs
BEGIN
    SELECT RAISE(ABORT, 'candidate_validation_runs is append-only');
END;
"""


_CANDIDATE_CONFLICT_DDL = """
CREATE TABLE IF NOT EXISTS candidate_semantic_keys (
    candidate_id TEXT NOT NULL,
    candidate_version INTEGER NOT NULL,
    canonical_key TEXT NOT NULL,
    value_fingerprint TEXT NOT NULL,
    canonical_candidate_id TEXT NOT NULL,
    canonical_candidate_version INTEGER NOT NULL,
    match_type TEXT NOT NULL DEFAULT 'unique' CHECK (match_type IN ('unique','distinct','exact_duplicate','near_duplicate','contradiction')),
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY(candidate_id, candidate_version),
    FOREIGN KEY(candidate_id, candidate_version) REFERENCES memory_candidates(candidate_id, candidate_version),
    FOREIGN KEY(canonical_candidate_id, canonical_candidate_version) REFERENCES memory_candidates(candidate_id, candidate_version)
);
CREATE INDEX IF NOT EXISTS idx_candidate_semantic_canonical ON candidate_semantic_keys(canonical_key, created_at);
CREATE TABLE IF NOT EXISTS candidate_evidence (
    candidate_id TEXT NOT NULL,
    candidate_version INTEGER NOT NULL,
    observation_id TEXT NOT NULL REFERENCES learning_observations(observation_id),
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY(candidate_id, candidate_version, observation_id),
    FOREIGN KEY(candidate_id, candidate_version) REFERENCES memory_candidates(candidate_id, candidate_version)
);
CREATE TABLE IF NOT EXISTS memory_conflicts (
    conflict_id TEXT PRIMARY KEY,
    canonical_key TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('unresolved','auto-resolved','review-resolved','superseded')),
    risk_level TEXT NOT NULL CHECK (risk_level IN ('low','medium','high','critical')),
    authority_blocked INTEGER NOT NULL DEFAULT 0 CHECK (authority_blocked IN (0,1)),
    superseded_by_candidate_id TEXT,
    superseded_by_version INTEGER,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at TEXT NOT NULL DEFAULT (datetime('now')),
    FOREIGN KEY(superseded_by_candidate_id, superseded_by_version) REFERENCES memory_candidates(candidate_id, candidate_version)
);
CREATE TABLE IF NOT EXISTS memory_conflict_members (
    conflict_id TEXT NOT NULL REFERENCES memory_conflicts(conflict_id),
    candidate_id TEXT NOT NULL,
    candidate_version INTEGER NOT NULL,
    PRIMARY KEY(conflict_id, candidate_id, candidate_version),
    FOREIGN KEY(candidate_id, candidate_version) REFERENCES memory_candidates(candidate_id, candidate_version)
);
CREATE TABLE IF NOT EXISTS memory_conflict_relations (
    relation_id INTEGER PRIMARY KEY AUTOINCREMENT,
    conflict_id TEXT NOT NULL REFERENCES memory_conflicts(conflict_id),
    left_candidate_id TEXT NOT NULL,
    left_candidate_version INTEGER NOT NULL,
    right_candidate_id TEXT NOT NULL,
    right_candidate_version INTEGER NOT NULL,
    relation_type TEXT NOT NULL CHECK (relation_type IN ('equivalent','opposes','supersedes')),
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE(conflict_id, left_candidate_id, left_candidate_version, right_candidate_id, right_candidate_version, relation_type),
    FOREIGN KEY(left_candidate_id, left_candidate_version) REFERENCES memory_candidates(candidate_id, candidate_version),
    FOREIGN KEY(right_candidate_id, right_candidate_version) REFERENCES memory_candidates(candidate_id, candidate_version)
);

CREATE TABLE IF NOT EXISTS memory_conflict_events (
    conflict_id TEXT NOT NULL REFERENCES memory_conflicts(conflict_id),
    event_seq INTEGER NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('unresolved','auto-resolved','review-resolved','superseded')),
    risk_level TEXT NOT NULL CHECK (risk_level IN ('low','medium','high','critical')),
    actor_id TEXT NOT NULL,
    superseded_by_candidate_id TEXT,
    superseded_by_version INTEGER,
    event_type TEXT NOT NULL DEFAULT 'status-changed' CHECK (event_type IN ('status-changed','risk-escalated')),
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY(conflict_id, event_seq),
    FOREIGN KEY(superseded_by_candidate_id, superseded_by_version) REFERENCES memory_candidates(candidate_id, candidate_version)
);
CREATE TRIGGER IF NOT EXISTS trg_memory_conflict_events_no_update BEFORE UPDATE ON memory_conflict_events BEGIN SELECT RAISE(ABORT, 'memory_conflict_events is append-only'); END;
CREATE TRIGGER IF NOT EXISTS trg_memory_conflict_events_no_delete BEFORE DELETE ON memory_conflict_events BEGIN SELECT RAISE(ABORT, 'memory_conflict_events is append-only'); END;
CREATE INDEX IF NOT EXISTS idx_memory_conflicts_key_status ON memory_conflicts(canonical_key, status, risk_level);
CREATE INDEX IF NOT EXISTS idx_memory_conflict_members_candidate ON memory_conflict_members(candidate_id, candidate_version);
"""

_LEARNING_OUTBOX_DDL = """
CREATE TABLE IF NOT EXISTS learning_outbox (
    job_id TEXT PRIMARY KEY,
    observation_id TEXT NOT NULL REFERENCES learning_observations(observation_id),
    job_type TEXT NOT NULL,
    extractor_version TEXT NOT NULL,
    state TEXT NOT NULL DEFAULT 'queued'
        CHECK (state IN ('queued','processing','retry','completed','quarantined','dead_letter')),
    attempts INTEGER NOT NULL DEFAULT 0 CHECK (attempts >= 0),
    available_at TEXT NOT NULL DEFAULT (datetime('now')),
    lease_token TEXT,
    leased_at TEXT,
    lease_expires_at TEXT,
    last_error TEXT,
    payload_json TEXT NOT NULL DEFAULT '{}',
    result_json TEXT,
    effect_key TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at TEXT NOT NULL DEFAULT (datetime('now')),
    completed_at TEXT,
    UNIQUE(observation_id, job_type, extractor_version)
);
CREATE INDEX IF NOT EXISTS idx_learning_outbox_due
    ON learning_outbox(state, available_at, lease_expires_at);
CREATE INDEX IF NOT EXISTS idx_learning_outbox_observation
    ON learning_outbox(observation_id, job_type, extractor_version);
CREATE TABLE IF NOT EXISTS learning_job_effects (
    effect_key TEXT PRIMARY KEY,
    job_id TEXT NOT NULL REFERENCES learning_outbox(job_id),
    observation_id TEXT NOT NULL REFERENCES learning_observations(observation_id),
    effect_type TEXT NOT NULL,
    result_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_learning_job_effects_job
    ON learning_job_effects(job_id, observation_id);
"""


def get_current_version(conn: sqlite3.Connection) -> int:
    """Get the current schema version from the database."""
    try:
        row = conn.execute(
            "SELECT MAX(version) FROM schema_version"
        ).fetchone()
        return row[0] if row and row[0] is not None else 0
    except sqlite3.OperationalError:
        return 0


def needs_migration(conn: sqlite3.Connection) -> bool:
    """Check if the database needs a schema migration."""
    return get_current_version(conn) < CURRENT_SCHEMA_VERSION


def record_migration(conn: sqlite3.Connection, version: int) -> None:
    """Record that a migration has been applied."""
    conn.execute(
        "INSERT INTO schema_version (version) VALUES (?)",
        (version,),
    )
    conn.commit()


def run_migrations(conn: sqlite3.Connection) -> None:
    """Apply released versions in order, then reconcile current table contracts."""
    from src.db.schema_version_guard import require_supported_schema_version

    current = require_supported_schema_version(conn)
    apply_migration_steps(
        conn, current=current, steps=_MIGRATION_STEPS,
        record=record_migration, logger=logger,
    )
    _reconcile_columns(conn)
    # v134: repair the v36 support-ticket table rebuild without rewriting its history.
    from src.db.support_ticket_repair import repair_support_tickets
    if repair_support_tickets(conn):
        logger.warning("reconcile: repaired support_tickets constraints and foreign keys")
    if current < 134:
        record_migration(conn, 134)
    # v135: v11's evidence-table rename retargeted a later child FK on v1 upgrades.
    from src.db.go_live_fk_repair import repair_go_live_evidence_fk
    if repair_go_live_evidence_fk(conn):
        logger.warning("reconcile: repaired go-live confirmation evidence foreign key")
    if current < 135:
        record_migration(conn, 135)
    # v136: rotate transient IMAP source retries without claiming a route.
    _migrate_v136(conn)
    if current < 136:
        record_migration(conn, 136)
    # v134-v136 are explicitly dispatched here, not absent migrations: keep
    # their historical reconciliation order after _MIGRATION_STEPS (through 133).
    # This late rebuild must follow all three even on a v1 upgrade.
    _migrate_v137(conn)
    if current < 137:
        record_migration(conn, 137)
    # Reconcile even if the version has been stamped by an older startup.
    _migrate_v138(conn)
    if current < 138:
        record_migration(conn, 138)
    # v139: committed CU-card intents, reconciled on already stamped databases.
    _migrate_v139(conn)
    if current < 139:
        record_migration(conn, 139)
    # v140: future committed advisory opportunities; no historical backfill.
    _migrate_v140(conn)
    if current < 140:
        record_migration(conn, 140)
    # v141: durable contact identity card intents; no backfill of old suggestions.
    _migrate_v141(conn)
    if current < 141:
        record_migration(conn, 141)
    # v142: provenance is explicit; repair missing columns and known inbound sources.
    _migrate_v142(conn)
    if current < 142:
        record_migration(conn, 142)
    _migrate_v143(conn)
    if current < 143:
        record_migration(conn, 143)


def _migrate_v2(conn: sqlite3.Connection) -> None:
    """
    v2: Add subject, sender_email, direction columns to processed_emails.
    These enable email processor to store richer metadata for triage tracking.
    Uses ALTER TABLE ADD COLUMN which is idempotent (columns silently ignored if already exist).
    """
    existing_cols = {
        row[1] for row in conn.execute("PRAGMA table_info(processed_emails)")
    }
    new_cols = [
        ("subject", "TEXT"),
        ("sender_email", "TEXT"),
        ("direction", "TEXT"),
    ]
    for col, typedef in new_cols:
        if col not in existing_cols:
            conn.execute(
                f"ALTER TABLE processed_emails ADD COLUMN {col} {typedef}"
            )
    conn.commit()


def _migrate_v3(conn: sqlite3.Connection) -> None:
    """v3: Add Telegram persistence tables."""
    from src.db.schema import apply_schema

    apply_schema(conn)


def _migrate_v4(conn: sqlite3.Connection) -> None:
    """v4: No-op. Superseded by v5."""
    pass


def _migrate_v5(conn: sqlite3.Connection) -> None:
    """v5: Add supervised contact suggestion storage via apply_schema."""
    from src.db.schema import apply_schema

    apply_schema(conn)


def _migrate_v6(conn: sqlite3.Connection) -> None:
    """v6: Add prompt_hash and per_guard_results columns to llm_traces."""
    existing_cols = {
        row[1] for row in conn.execute("PRAGMA table_info(llm_traces)")
    }
    for col, typedef in [("prompt_hash", "TEXT"), ("per_guard_results", "TEXT")]:
        if col not in existing_cols:
            conn.execute(f"ALTER TABLE llm_traces ADD COLUMN {col} {typedef}")
    conn.commit()


def _migrate_v7(conn: sqlite3.Connection) -> None:
    """v7: Add internal contact fields to contacts table."""
    existing_cols = {
        row[1] for row in conn.execute("PRAGMA table_info(contacts)")
    }
    new_cols = [
        ("department", "TEXT"),
        ("expertise_tags", "TEXT DEFAULT '[]'"),
        ("responsiveness_score", "REAL"),
        ("preferred_contact_method", "TEXT"),
    ]
    for col, typedef in new_cols:
        if col not in existing_cols:
            conn.execute(f"ALTER TABLE contacts ADD COLUMN {col} {typedef}")
    conn.commit()


def _migrate_v8(conn: sqlite3.Connection) -> None:
    """v8: Add project_tier column to projects table."""
    existing_cols = {row[1] for row in conn.execute("PRAGMA table_info(projects)")}
    if "project_tier" not in existing_cols:
        conn.execute("ALTER TABLE projects ADD COLUMN project_tier TEXT")
    conn.commit()


def _migrate_v9(conn: sqlite3.Connection) -> None:
    """v9: Create closing_checklist table."""
    from src.db.schema import apply_schema
    apply_schema(conn)


def _migrate_v10(conn: sqlite3.Connection) -> None:
    """v10: Add thread_id, in_reply_to, references_chain to processed_emails + inbound_replies table."""
    existing_cols = {row[1] for row in conn.execute("PRAGMA table_info(processed_emails)")}
    for col, typedef in [
        ("thread_id", "TEXT"),
        ("in_reply_to", "TEXT"),
        ("references_chain", "TEXT"),
    ]:
        if col not in existing_cols:
            conn.execute(f"ALTER TABLE processed_emails ADD COLUMN {col} {typedef}")
    conn.commit()
    from src.db.schema import apply_schema
    apply_schema(conn)


def _migrate_v11(conn: sqlite3.Connection) -> None:
    """v11: Recreate evidence_store with updated CHECK constraint to include 'manual_note'."""
    # Check if already using the updated constraint by testing the new value
    try:
        conn.execute(
            "INSERT INTO evidence_store (evidence_id, project_id, evidence_type, "
            "content_summary, evidence_at) VALUES ('_chk', '_chk', 'manual_note', '_chk', '_chk')"
        )
        conn.execute("DELETE FROM evidence_store WHERE evidence_id = '_chk'")
        conn.commit()
        return  # Already supports manual_note
    except Exception:
        conn.rollback()

    # Recreate using rename → create → copy → drop pattern
    conn.execute("ALTER TABLE evidence_store RENAME TO evidence_store_old")
    from src.db.schema import apply_schema
    apply_schema(conn)  # creates evidence_store with updated CHECK
    conn.execute(
        "INSERT INTO evidence_store SELECT * FROM evidence_store_old"
    )
    conn.execute("DROP TABLE evidence_store_old")
    conn.commit()


def _migrate_v12(conn: sqlite3.Connection) -> None:
    """v12: Create operator_interaction_log table."""
    from src.db.schema import apply_schema
    apply_schema(conn)


def _migrate_v13(conn: sqlite3.Connection) -> None:
    """v13: Add notification_mode column to operator_state."""
    existing_cols = {row[1] for row in conn.execute("PRAGMA table_info(operator_state)")}
    if "notification_mode" not in existing_cols:
        conn.execute(
            "ALTER TABLE operator_state ADD COLUMN notification_mode TEXT DEFAULT 'active'"
        )
    conn.commit()


def _migrate_v14(conn: sqlite3.Connection) -> None:
    """v14: Create ai_feedback table + add budget columns to operator_state."""
    from src.db.schema import apply_schema
    apply_schema(conn)  # picks up ai_feedback CREATE TABLE IF NOT EXISTS

    existing_cols = {row[1] for row in conn.execute("PRAGMA table_info(operator_state)")}
    new_cols = [
        ("max_daily_notifications", "INTEGER DEFAULT 15"),
        ("notification_delivery_mode", "TEXT DEFAULT 'realtime'"),
        ("quiet_hours_start", "TEXT"),
        ("quiet_hours_end", "TEXT"),
        ("notifications_sent_today", "INTEGER DEFAULT 0"),
        ("notifications_reset_date", "TEXT"),
    ]
    for col, typedef in new_cols:
        if col not in existing_cols:
            conn.execute(f"ALTER TABLE operator_state ADD COLUMN {col} {typedef}")
    conn.commit()


def _repair_learning_job_effects_fk(conn: sqlite3.Connection) -> bool:
    """Rebuild learning_job_effects when a legacy outbox rename poisoned its FK."""
    columns = conn.execute("PRAGMA table_info(learning_job_effects)").fetchall()
    if not columns:
        return False
    foreign_keys = conn.execute("PRAGMA foreign_key_list(learning_job_effects)").fetchall()
    job_fk = next((row for row in foreign_keys if row[3] == "job_id"), None)
    if job_fk is not None and job_fk[2] == "learning_outbox" and job_fk[4] == "job_id":
        return False

    conn.commit()
    conn.execute("PRAGMA foreign_keys = OFF")
    try:
        conn.executescript(
            """
            DROP INDEX IF EXISTS idx_learning_job_effects_job;
            ALTER TABLE learning_job_effects RENAME TO learning_job_effects_legacy_fk;
            CREATE TABLE learning_job_effects (
                effect_key TEXT PRIMARY KEY,
                job_id TEXT NOT NULL REFERENCES learning_outbox(job_id),
                observation_id TEXT NOT NULL REFERENCES learning_observations(observation_id),
                effect_type TEXT NOT NULL,
                result_json TEXT NOT NULL DEFAULT '{}',
                created_at TEXT NOT NULL DEFAULT (datetime('now'))
            );
            INSERT INTO learning_job_effects(
                effect_key, job_id, observation_id, effect_type, result_json, created_at
            )
            SELECT effect_key, job_id, observation_id, effect_type, result_json, created_at
            FROM learning_job_effects_legacy_fk;
            DROP TABLE learning_job_effects_legacy_fk;
            CREATE INDEX idx_learning_job_effects_job
                ON learning_job_effects(job_id, observation_id);
            """
        )
    finally:
        conn.execute("PRAGMA foreign_keys = ON")
    violations = conn.execute("PRAGMA foreign_key_check(learning_job_effects)").fetchall()
    if violations:
        raise RuntimeError(f"learning_job_effects FK repair failed: {violations}")
    logger.warning("reconcile: repaired learning_job_effects.job_id foreign key")
    return True


def _reconcile_columns(conn: sqlite3.Connection) -> None:
    """
    Safety net: unconditionally ensure every ALTER TABLE column from all migrations
    exists, regardless of what version is recorded in schema_version.
    Guards against DB re-inits or WAL races that stamp the version without persisting
    the column changes.
    """
    changed = False

    # v67-v68: candidate proposals and deterministic validation records.
    candidate_cols = {r[1] for r in conn.execute("PRAGMA table_info(memory_candidates)")}
    if not candidate_cols:
        conn.executescript(_MEMORY_CANDIDATES_DDL)
    if candidate_cols and "revision_of_validation_id" not in candidate_cols:
        _migrate_v68(conn)
    conn.executescript(_CANDIDATE_GOVERNANCE_DDL)
    conn.executescript(_CANDIDATE_CONFLICT_DDL)
    semantic_cols = {row[1] for row in conn.execute("PRAGMA table_info(candidate_semantic_keys)")}
    if semantic_cols and "match_type" not in semantic_cols:
        conn.execute("ALTER TABLE candidate_semantic_keys ADD COLUMN match_type TEXT NOT NULL DEFAULT 'unique'")
    event_cols = {row[1] for row in conn.execute("PRAGMA table_info(memory_conflict_events)")}
    if event_cols and "event_type" not in event_cols:
        conn.execute("ALTER TABLE memory_conflict_events ADD COLUMN event_type TEXT NOT NULL DEFAULT 'status-changed'")

    # v2: processed_emails
    pe_cols = {r[1] for r in conn.execute("PRAGMA table_info(processed_emails)")}
    for col, typedef in [("subject", "TEXT"), ("sender_email", "TEXT"), ("direction", "TEXT")]:
        if col not in pe_cols:
            conn.execute(f"ALTER TABLE processed_emails ADD COLUMN {col} {typedef}")
            logger.warning("reconcile: added processed_emails.%s", col)
            changed = True

    # v98: processed_emails source timestamp and structured recipient evidence
    pe_cols98 = {r[1] for r in conn.execute("PRAGMA table_info(processed_emails)")}
    for col, typedef in [
        ("received_at", "TEXT"),
        ("to_recipients_json", "TEXT NOT NULL DEFAULT '[]'"),
        ("cc_recipients_json", "TEXT NOT NULL DEFAULT '[]'"),
        ("bcc_recipients_json", "TEXT NOT NULL DEFAULT '[]'"),
    ]:
        if col not in pe_cols98:
            conn.execute(f"ALTER TABLE processed_emails ADD COLUMN {col} {typedef}")
            logger.warning("reconcile: added processed_emails.%s", col)
            changed = True
    conn.execute(
        """UPDATE processed_emails SET received_at=processed_at
           WHERE received_at IS NULL OR trim(received_at)=''"""
    )
    conn.execute(
        """CREATE INDEX IF NOT EXISTS idx_processed_emails_project_received
           ON processed_emails(project_id, received_at DESC)"""
    )

    # v6: llm_traces
    lt_cols = {r[1] for r in conn.execute("PRAGMA table_info(llm_traces)")}
    for col, typedef in [("prompt_hash", "TEXT"), ("per_guard_results", "TEXT")]:
        if col not in lt_cols:
            conn.execute(f"ALTER TABLE llm_traces ADD COLUMN {col} {typedef}")
            logger.warning("reconcile: added llm_traces.%s", col)
            changed = True

    # v7: contacts
    ct_cols = {r[1] for r in conn.execute("PRAGMA table_info(contacts)")}
    for col, typedef in [
        ("department", "TEXT"),
        ("expertise_tags", "TEXT DEFAULT '[]'"),
        ("responsiveness_score", "REAL"),
        ("preferred_contact_method", "TEXT"),
    ]:
        if col not in ct_cols:
            conn.execute(f"ALTER TABLE contacts ADD COLUMN {col} {typedef}")
            logger.warning("reconcile: added contacts.%s", col)
            changed = True

    # v8: projects
    pr_cols = {r[1] for r in conn.execute("PRAGMA table_info(projects)")}
    if "project_tier" not in pr_cols:
        conn.execute("ALTER TABLE projects ADD COLUMN project_tier TEXT")
        logger.warning("reconcile: added projects.project_tier")
        changed = True

    # v10: processed_emails threading columns
    pe_cols2 = {r[1] for r in conn.execute("PRAGMA table_info(processed_emails)")}
    for col in ["thread_id", "in_reply_to", "references_chain"]:
        if col not in pe_cols2:
            conn.execute(f"ALTER TABLE processed_emails ADD COLUMN {col} TEXT")
            logger.warning("reconcile: added processed_emails.%s", col)
            changed = True

    # v13: operator_state
    os_cols = {r[1] for r in conn.execute("PRAGMA table_info(operator_state)")}
    if "notification_mode" not in os_cols:
        conn.execute(
            "ALTER TABLE operator_state ADD COLUMN notification_mode TEXT DEFAULT 'active'"
        )
        logger.warning("reconcile: added operator_state.notification_mode")
        changed = True

    # v14: operator_state budget columns
    os_cols2 = {r[1] for r in conn.execute("PRAGMA table_info(operator_state)")}
    for col, typedef in [
        ("max_daily_notifications", "INTEGER DEFAULT 15"),
        ("notification_delivery_mode", "TEXT DEFAULT 'realtime'"),
        ("quiet_hours_start", "TEXT"),
        ("quiet_hours_end", "TEXT"),
        ("notifications_sent_today", "INTEGER DEFAULT 0"),
        ("notifications_reset_date", "TEXT"),
    ]:
        if col not in os_cols2:
            conn.execute(f"ALTER TABLE operator_state ADD COLUMN {col} {typedef}")
            logger.warning("reconcile: added operator_state.%s", col)
            changed = True

    # v104: durable feedback linkage to engineering runs and output revisions.
    feedback_cols = {r[1] for r in conn.execute("PRAGMA table_info(ai_feedback)")}
    for col in ("surface", "trace_id", "run_id", "turn_id", "output_revision_id"):
        if col not in feedback_cols:
            conn.execute(f"ALTER TABLE ai_feedback ADD COLUMN {col} TEXT")
            logger.warning("reconcile: added ai_feedback.%s", col)
            changed = True
    conn.execute("CREATE INDEX IF NOT EXISTS idx_ai_feedback_trace_id ON ai_feedback(trace_id)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_ai_feedback_run_id ON ai_feedback(run_id)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_ai_feedback_turn_id ON ai_feedback(turn_id)")

    os_cols_h = {r[1] for r in conn.execute("PRAGMA table_info(operator_state)")}
    for col in ("harness_require_wiki_progress", "harness_auto_draft_rule58"):
        if col not in os_cols_h:
            conn.execute(f"ALTER TABLE operator_state ADD COLUMN {col} INTEGER")
            logger.warning("reconcile: added operator_state.%s", col)
            changed = True

    # v19: support_tickets summary/draft columns
    st_cols_v19 = {r[1] for r in conn.execute("PRAGMA table_info(support_tickets)")}
    for col in ["reply_draft_text", "draft_generated_at", "ai_summary_json"]:
        if col not in st_cols_v19:
            conn.execute(f"ALTER TABLE support_tickets ADD COLUMN {col} TEXT")
            logger.warning("reconcile: added support_tickets.%s", col)
            changed = True

    # v20: processed_emails.ticket_id
    pe_cols3 = {r[1] for r in conn.execute("PRAGMA table_info(processed_emails)")}
    if "ticket_id" not in pe_cols3:
        conn.execute(
            "ALTER TABLE processed_emails ADD COLUMN ticket_id TEXT "
            "REFERENCES support_tickets(ticket_id)"
        )
        logger.warning("reconcile: added processed_emails.ticket_id")
        changed = True

    # v29: processed_emails knowledge mining columns
    pe_cols4 = {r[1] for r in conn.execute("PRAGMA table_info(processed_emails)")}
    for col, typedef in [("body_snippet", "TEXT"), ("knowledge_mined", "INTEGER DEFAULT 0")]:
        if col not in pe_cols4:
            conn.execute(f"ALTER TABLE processed_emails ADD COLUMN {col} {typedef}")
            logger.warning("reconcile: added processed_emails.%s", col)
            changed = True

    # v63: durable learning and retryable email mining
    pe_cols63 = {r[1] for r in conn.execute("PRAGMA table_info(processed_emails)")}
    for col, typedef in [
        ("knowledge_mining_status", "TEXT NOT NULL DEFAULT 'pending'"),
        ("knowledge_mining_attempts", "INTEGER NOT NULL DEFAULT 0"),
        ("knowledge_mining_error", "TEXT"),
        ("knowledge_mining_lease_owner", "TEXT"),
        ("knowledge_mining_lease_expires_at", "TEXT"),
        ("knowledge_mined_at", "TEXT"),
    ]:
        if col not in pe_cols63:
            conn.execute(f"ALTER TABLE processed_emails ADD COLUMN {col} {typedef}")
            logger.warning("reconcile: added processed_emails.%s", col)
            changed = True
    conn.executescript(_LEARNING_OUTBOX_DDL)
    if _repair_learning_job_effects_fk(conn):
        changed = True

    # v21: support_tickets.thread_anchor
    st_cols = {r[1] for r in conn.execute("PRAGMA table_info(support_tickets)")}
    if "thread_anchor" not in st_cols:
        conn.execute("ALTER TABLE support_tickets ADD COLUMN thread_anchor TEXT")
        logger.warning("reconcile: added support_tickets.thread_anchor")
        changed = True

    # v22: drafts.ticket_id
    dr_cols = {r[1] for r in conn.execute("PRAGMA table_info(drafts)")}
    if "ticket_id" not in dr_cols:
        conn.execute(
            "ALTER TABLE drafts ADD COLUMN ticket_id TEXT "
            "REFERENCES support_tickets(ticket_id)"
        )
        logger.warning("reconcile: added drafts.ticket_id")
        changed = True

    # v34: drafts.scheduled_send_at
    dr_cols2 = {r[1] for r in conn.execute("PRAGMA table_info(drafts)")}
    if "scheduled_send_at" not in dr_cols2:
        conn.execute("ALTER TABLE drafts ADD COLUMN scheduled_send_at TEXT")
        logger.warning("reconcile: added drafts.scheduled_send_at")
        changed = True

    # v23: learned_rules.updated_at
    lr_cols = {r[1] for r in conn.execute("PRAGMA table_info(learned_rules)")}
    if "updated_at" not in lr_cols:
        conn.execute("ALTER TABLE learned_rules ADD COLUMN updated_at TEXT")
        conn.execute("UPDATE learned_rules SET updated_at = created_at WHERE updated_at IS NULL")
        logger.warning("reconcile: added learned_rules.updated_at")
        changed = True

    # v30: llm_traces.trace_metadata
    lt_cols = {r[1] for r in conn.execute("PRAGMA table_info(llm_traces)")}
    if "trace_metadata" not in lt_cols:
        conn.execute("ALTER TABLE llm_traces ADD COLUMN trace_metadata TEXT")
        logger.warning("reconcile: added llm_traces.trace_metadata")
        changed = True

    # v31: agent_notes.note_type
    an_cols = {r[1] for r in conn.execute("PRAGMA table_info(agent_notes)")}
    if "note_type" not in an_cols:
        conn.execute("ALTER TABLE agent_notes ADD COLUMN note_type TEXT NOT NULL DEFAULT 'knowledge'")
        logger.warning("reconcile: added agent_notes.note_type")
        changed = True

    # v33: operator_state.response_language
    os_cols3 = {r[1] for r in conn.execute("PRAGMA table_info(operator_state)")}
    if "response_language" not in os_cols3:
        conn.execute(
            "ALTER TABLE operator_state ADD COLUMN response_language TEXT DEFAULT 'zh-TW'"
        )
        logger.warning("reconcile: added operator_state.response_language")
        changed = True

    # v37: work_item_id columns
    _ensure_col(conn, "projects",        "work_item_id", "TEXT")
    _ensure_col(conn, "support_tickets", "work_item_id", "TEXT")
    _ensure_col(conn, "drafts",          "work_item_id", "TEXT")

    # v38: inbound_replies
    ir_cols = {r[1] for r in conn.execute("PRAGMA table_info(inbound_replies)")}
    if "requested_docs_json" not in ir_cols:
        conn.execute(
            "ALTER TABLE inbound_replies ADD COLUMN requested_docs_json TEXT"
        )
        logger.warning("reconcile: added inbound_replies.requested_docs_json")
        changed = True

    # v41: email_knowledge_log.status
    _ensure_col(
        conn, "email_knowledge_log", "status",
        "TEXT NOT NULL DEFAULT 'active'"
    )

    # v27: fresh and stamped databases both retain the configurable timeout.
    _ensure_col(conn, "operator_state", "session_timeout_minutes", "INTEGER DEFAULT 10")

    # v42: operator_state.thinking_window_enabled
    _ensure_col(conn, "operator_state", "thinking_window_enabled", "INTEGER NOT NULL DEFAULT 0")

    # v50: contact_suggestions party-graph columns (may be missing if only tables ran)
    _ensure_col(conn, "contact_suggestions", "org_type_guess", "TEXT DEFAULT 'unknown'")
    _ensure_col(conn, "contact_suggestions", "last_pushed_at", "TEXT")

    # v51: support ticket party graph link
    _ensure_col(conn, "support_tickets", "counterparty_org_id", "TEXT")

    # v65-v66: observation ledger and durable outbox may be absent on a stamped DB.
    from src.db.schema import apply_schema
    apply_schema(conn)
    outbox_cols = {r[1] for r in conn.execute("PRAGMA table_info(learning_outbox)")}
    if outbox_cols and "job_id" not in outbox_cols:
        _migrate_v66(conn)
    conn.executescript("""
        CREATE TRIGGER IF NOT EXISTS trg_learning_observations_no_update
        BEFORE UPDATE ON learning_observations
        BEGIN
            SELECT RAISE(ABORT, 'learning_observations is append-only');
        END;
        CREATE TRIGGER IF NOT EXISTS trg_learning_observations_no_delete
        BEFORE DELETE ON learning_observations
        BEGIN
            SELECT RAISE(ABORT, 'learning_observations is append-only');
        END;
    """)

    # v64: proposed-update assessment metadata
    pu_cols = {r[1] for r in conn.execute("PRAGMA table_info(proposed_updates)")}
    for col, typedef in [
        ("assessment_json", "TEXT"),
        ("policy_version", "TEXT"),
        ("source_type", "TEXT"),
        ("source_ref", "TEXT"),
    ]:
        if col not in pu_cols:
            conn.execute(f"ALTER TABLE proposed_updates ADD COLUMN {col} {typedef}")
            logger.warning("reconcile: added proposed_updates.%s", col)
            changed = True

    # v108: team identity / later four-eyes hooks
    pu_cols108 = {r[1] for r in conn.execute("PRAGMA table_info(proposed_updates)")}
    if pu_cols108 and "decided_by" not in pu_cols108:
        conn.execute("ALTER TABLE proposed_updates ADD COLUMN decided_by TEXT")
        logger.warning("reconcile: added proposed_updates.decided_by")
        changed = True
    draft_cols108 = {r[1] for r in conn.execute("PRAGMA table_info(drafts)")}
    for col, typedef in [("claimed_by", "TEXT"), ("claimed_at", "TEXT")]:
        if draft_cols108 and col not in draft_cols108:
            conn.execute(f"ALTER TABLE drafts ADD COLUMN {col} {typedef}")
            logger.warning("reconcile: added drafts.%s", col)
            changed = True
    conn.execute(
        """CREATE INDEX IF NOT EXISTS idx_external_identities_provider_object
           ON external_identities(provider, object_id)"""
    )
    conn.execute(
        """CREATE INDEX IF NOT EXISTS idx_drafts_claimed_by
           ON drafts(claimed_by, claimed_at)"""
    )

    if changed:
        conn.commit()


def _migrate_v15(conn: sqlite3.Connection) -> None:
    """v15: Create support_tickets, ticket_evidence, and resolution_patterns tables."""
    from src.db.schema import apply_schema
    apply_schema(conn)


def _migrate_v16(conn: sqlite3.Connection) -> None:
    """v16: Add missing indexes on resolution_patterns and telegram_sessions."""
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_resolution_patterns_category "
        "ON resolution_patterns(category)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_resolution_patterns_confidence "
        "ON resolution_patterns(confidence)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_telegram_sessions_flow_status "
        "ON telegram_sessions(flow_type, status)"
    )
    conn.commit()


def _migrate_v17(conn: sqlite3.Connection) -> None:
    """v17: Add reply_draft_text and draft_generated_at to support_tickets."""
    existing = [r[1] for r in conn.execute("PRAGMA table_info(support_tickets)").fetchall()]
    if "reply_draft_text" not in existing:
        conn.execute("ALTER TABLE support_tickets ADD COLUMN reply_draft_text TEXT")
    if "draft_generated_at" not in existing:
        conn.execute("ALTER TABLE support_tickets ADD COLUMN draft_generated_at TEXT")
    conn.commit()


def _migrate_v18(conn: sqlite3.Connection) -> None:
    """v18: Add support_chat_messages for persistent support agent chat history."""
    conn.execute(
        """CREATE TABLE IF NOT EXISTS support_chat_messages (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            ticket_id   TEXT NOT NULL REFERENCES support_tickets(ticket_id) ON DELETE CASCADE,
            role        TEXT NOT NULL CHECK(role IN ('user', 'assistant')),
            content     TEXT NOT NULL,
            created_at  TEXT NOT NULL DEFAULT (datetime('now'))
        )"""
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_support_chat_ticket "
        "ON support_chat_messages(ticket_id, created_at)"
    )
    conn.commit()


def _migrate_v19(conn: sqlite3.Connection) -> None:
    """v19: Add ai_summary_json to support_tickets for cached AI summaries."""
    existing = [r[1] for r in conn.execute("PRAGMA table_info(support_tickets)").fetchall()]
    if "ai_summary_json" not in existing:
        conn.execute("ALTER TABLE support_tickets ADD COLUMN ai_summary_json TEXT")
    conn.commit()


def _migrate_v20(conn: sqlite3.Connection) -> None:
    """v20: Add ticket_id to processed_emails for explicit email-to-ticket linkage."""
    existing = [r[1] for r in conn.execute("PRAGMA table_info(processed_emails)").fetchall()]
    if "ticket_id" not in existing:
        conn.execute(
            "ALTER TABLE processed_emails ADD COLUMN ticket_id TEXT "
            "REFERENCES support_tickets(ticket_id)"
        )
    conn.commit()


def _migrate_v21(conn: sqlite3.Connection) -> None:
    """v21: Add thread_anchor to support_tickets for email thread-based linking."""
    existing = [r[1] for r in conn.execute("PRAGMA table_info(support_tickets)").fetchall()]
    if "thread_anchor" not in existing:
        conn.execute("ALTER TABLE support_tickets ADD COLUMN thread_anchor TEXT")
    conn.commit()


def _migrate_v23(conn: sqlite3.Connection) -> None:
    """v23: Add updated_at column to learned_rules for confidence lifecycle."""
    existing_cols = {row[1] for row in conn.execute("PRAGMA table_info(learned_rules)")}
    if "updated_at" not in existing_cols:
        conn.execute("ALTER TABLE learned_rules ADD COLUMN updated_at TEXT")
        conn.execute("UPDATE learned_rules SET updated_at = created_at WHERE updated_at IS NULL")
    conn.commit()


def _migrate_v22(conn: sqlite3.Connection) -> None:
    """v22: Add ticket_id column to drafts for support ticket linkage."""
    existing_cols = {
        row[1] for row in conn.execute("PRAGMA table_info(drafts)")
    }
    if "ticket_id" not in existing_cols:
        conn.execute(
            "ALTER TABLE drafts ADD COLUMN ticket_id TEXT REFERENCES support_tickets(ticket_id)"
        )
    conn.commit()


def _migrate_v25(conn: sqlite3.Connection) -> None:
    """Add project_corrections table (L3 agent self-correction memory)."""
    conn.execute("""
        CREATE TABLE IF NOT EXISTS project_corrections (
            correction_id TEXT PRIMARY KEY,
            project_id TEXT NOT NULL REFERENCES projects(project_id),
            field TEXT NOT NULL,
            wrong_value TEXT NOT NULL,
            correct_value TEXT NOT NULL,
            source TEXT NOT NULL DEFAULT 'agent_self_correction',
            created_at TEXT NOT NULL DEFAULT (datetime('now'))
        )
    """)
    conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_project_corrections_project
            ON project_corrections(project_id, created_at DESC)
    """)
    conn.commit()


def _migrate_v26(conn: sqlite3.Connection) -> None:
    """Add project_chat_messages table for dashboard agent chat."""
    conn.execute("""
        CREATE TABLE IF NOT EXISTS project_chat_messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            project_id TEXT NOT NULL,
            role TEXT NOT NULL CHECK(role IN ('user', 'assistant')),
            content TEXT NOT NULL,
            created_at TEXT NOT NULL DEFAULT (datetime('now'))
        )
    """)
    conn.commit()


def _migrate_v27(conn: sqlite3.Connection) -> None:
    """Add session_timeout_minutes column to operator_state (D3 configurable timeout)."""
    try:
        conn.execute("ALTER TABLE operator_state ADD COLUMN session_timeout_minutes INTEGER DEFAULT 10")
    except Exception:
        pass  # column already exists
    conn.commit()


def _migrate_v28(conn: sqlite3.Connection) -> None:
    """Add agent_notes table + FTS5 index on session_summaries."""
    # agent_notes table (idempotent via IF NOT EXISTS)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS agent_notes (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            note_key TEXT UNIQUE NOT NULL,
            note_value TEXT NOT NULL,
            updated_at TEXT NOT NULL DEFAULT (datetime('now'))
        )
    """)
    # FTS5 virtual table for session summary search
    conn.execute("""
        CREATE VIRTUAL TABLE IF NOT EXISTS session_summaries_fts USING fts5(
            summary_text,
            projects_discussed,
            decisions_made,
            open_questions,
            content='session_summaries',
            content_rowid='rowid'
        )
    """)
    # Sync triggers
    conn.executescript("""
        CREATE TRIGGER IF NOT EXISTS session_summaries_ai AFTER INSERT ON session_summaries BEGIN
            INSERT INTO session_summaries_fts(rowid, summary_text, projects_discussed, decisions_made, open_questions)
            VALUES (new.rowid, new.summary_text, new.projects_discussed, new.decisions_made, new.open_questions);
        END;

        CREATE TRIGGER IF NOT EXISTS session_summaries_ad AFTER DELETE ON session_summaries BEGIN
            INSERT INTO session_summaries_fts(session_summaries_fts, rowid, summary_text, projects_discussed, decisions_made, open_questions)
            VALUES ('delete', old.rowid, old.summary_text, old.projects_discussed, old.decisions_made, old.open_questions);
        END;

        CREATE TRIGGER IF NOT EXISTS session_summaries_au AFTER UPDATE ON session_summaries BEGIN
            INSERT INTO session_summaries_fts(session_summaries_fts, rowid, summary_text, projects_discussed, decisions_made, open_questions)
            VALUES ('delete', old.rowid, old.summary_text, old.projects_discussed, old.decisions_made, old.open_questions);
            INSERT INTO session_summaries_fts(rowid, summary_text, projects_discussed, decisions_made, open_questions)
            VALUES (new.rowid, new.summary_text, new.projects_discussed, new.decisions_made, new.open_questions);
        END;
    """)
    # Backfill existing session summaries into FTS index
    conn.execute("""
        INSERT OR IGNORE INTO session_summaries_fts(rowid, summary_text, projects_discussed, decisions_made, open_questions)
        SELECT rowid, summary_text,
               COALESCE(projects_discussed, ''),
               COALESCE(decisions_made, ''),
               COALESCE(open_questions, '')
        FROM session_summaries
    """)
    conn.commit()


def _migrate_v30(conn: sqlite3.Connection) -> None:
    """Deduplicate learned_rules + add trace_metadata column to llm_traces."""
    import re as _re

    # --- Dedup learned_rules in Python ---
    rows = conn.execute(
        "SELECT rule_id, rule, created_at, usage_count, confidence FROM learned_rules"
    ).fetchall()

    def _normalize(text: str) -> str:
        text = (text or "").lower().strip()
        text = _re.sub(r"\s+", " ", text)
        return text

    # Group by normalized rule text — keep oldest (MIN created_at) per group
    groups: dict[str, list[dict]] = {}
    for row in rows:
        d = dict(row)
        key = _normalize(d["rule"])
        if not key:
            continue
        groups.setdefault(key, []).append(d)

    for key, members in groups.items():
        if len(members) < 2:
            continue
        # Sort by created_at ASC — survivor is the oldest
        members.sort(key=lambda m: m["created_at"] or "")
        survivor = members[0]
        total_usage = sum(m["usage_count"] or 0 for m in members)
        max_conf = max(m["confidence"] or 0 for m in members)
        # Update survivor with aggregated values
        conn.execute(
            "UPDATE learned_rules SET usage_count = ?, confidence = ? WHERE rule_id = ?",
            (total_usage, max_conf, survivor["rule_id"]),
        )
        # Delete the rest
        duplicate_ids = [m["rule_id"] for m in members[1:]]
        for dup_id in duplicate_ids:
            conn.execute("DELETE FROM learned_rules WHERE rule_id = ?", (dup_id,))

    # --- Add trace_metadata column to llm_traces (idempotent) ---
    lt_cols = {r[1] for r in conn.execute("PRAGMA table_info(llm_traces)")}
    if "trace_metadata" not in lt_cols:
        conn.execute("ALTER TABLE llm_traces ADD COLUMN trace_metadata TEXT")

    conn.commit()


def _migrate_v29(conn: sqlite3.Connection) -> None:
    """Add body_snippet + knowledge_mined to processed_emails; create email_knowledge_log."""
    pe_cols = {r[1] for r in conn.execute("PRAGMA table_info(processed_emails)")}
    for col, typedef in [("body_snippet", "TEXT"), ("knowledge_mined", "INTEGER DEFAULT 0")]:
        if col not in pe_cols:
            conn.execute(f"ALTER TABLE processed_emails ADD COLUMN {col} {typedef}")
    conn.execute("""
        CREATE TABLE IF NOT EXISTS email_knowledge_log (
            entry_id TEXT PRIMARY KEY,
            created_at TEXT NOT NULL,
            thread_id TEXT,
            project_id TEXT,
            ticket_id TEXT,
            knowledge_type TEXT NOT NULL CHECK (knowledge_type IN ('domain_fact', 'problem_solution', 'process_step')),
            title TEXT NOT NULL,
            content TEXT NOT NULL,
            confidence TEXT NOT NULL DEFAULT 'medium' CHECK (confidence IN ('high', 'medium', 'low')),
            visibility TEXT NOT NULL DEFAULT 'public' CHECK (visibility IN ('public', 'internal_only')),
            wiki_path TEXT,
            source_emails TEXT NOT NULL DEFAULT '[]',
            mining_run_id TEXT NOT NULL
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_email_knowledge_log_created ON email_knowledge_log(created_at DESC)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_email_knowledge_log_project ON email_knowledge_log(project_id)")
    conn.commit()


def _migrate_v31(conn: sqlite3.Connection) -> None:
    """v31: Add note_type column to agent_notes with auto-classification."""
    cols = {r[1] for r in conn.execute("PRAGMA table_info(agent_notes)")}
    if "note_type" not in cols:
        conn.execute(
            "ALTER TABLE agent_notes ADD COLUMN note_type TEXT NOT NULL DEFAULT 'knowledge'"
        )
        # Heuristic: classify existing notes with project ID patterns as diary
        conn.execute("""
            UPDATE agent_notes SET note_type = 'diary'
            WHERE note_key GLOB '*[0-9][0-9][0-9][0-9]*'
              AND note_key NOT LIKE '%workflow%'
              AND note_key NOT LIKE '%behavior%'
              AND note_key NOT LIKE '%preference%'
        """)
    conn.commit()


def _migrate_v32(conn: sqlite3.Connection) -> None:
    """v32: Expand agent_notes note_type CHECK to include preference/pattern/correction."""
    # Test if the new values are already accepted
    try:
        conn.execute(
            "INSERT INTO agent_notes (note_key, note_value, note_type) "
            "VALUES ('_chk_v32', '_chk', 'preference')"
        )
        conn.execute("DELETE FROM agent_notes WHERE note_key = '_chk_v32'")
        conn.commit()
        return  # Already supports new types
    except Exception:
        conn.rollback()

    # Recreate using rename -> create -> copy -> drop pattern
    conn.execute("ALTER TABLE agent_notes RENAME TO agent_notes_old")
    conn.execute("""
        CREATE TABLE IF NOT EXISTS agent_notes (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            note_key TEXT UNIQUE NOT NULL,
            note_value TEXT NOT NULL,
            note_type TEXT NOT NULL DEFAULT 'knowledge'
                CHECK (note_type IN ('knowledge', 'diary', 'preference', 'pattern', 'correction')),
            updated_at TEXT NOT NULL DEFAULT (datetime('now'))
        )
    """)
    conn.execute(
        "INSERT INTO agent_notes (id, note_key, note_value, note_type, updated_at) "
        "SELECT id, note_key, note_value, note_type, updated_at FROM agent_notes_old"
    )
    conn.execute("DROP TABLE agent_notes_old")
    conn.commit()


def _migrate_v33(conn: sqlite3.Connection) -> None:
    """v33: Add response_language column to operator_state."""
    existing_cols = {row[1] for row in conn.execute("PRAGMA table_info(operator_state)")}
    if "response_language" not in existing_cols:
        conn.execute(
            "ALTER TABLE operator_state ADD COLUMN response_language TEXT DEFAULT 'zh-TW'"
        )
    conn.commit()


def _migrate_v34(conn: sqlite3.Connection) -> None:
    """v34: Add scheduled_send_at column to drafts for send-later scheduling."""
    existing_cols = {row[1] for row in conn.execute("PRAGMA table_info(drafts)")}
    if "scheduled_send_at" not in existing_cols:
        conn.execute("ALTER TABLE drafts ADD COLUMN scheduled_send_at TEXT")
    conn.commit()


def _migrate_v35(conn: sqlite3.Connection) -> None:
    """v35: Create digest_queue table for batched notification delivery."""
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS digest_queue (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            category TEXT NOT NULL CHECK (category IN (
                'sla_alert','followup_overdue','ticket_aging',
                'email_ambiguous','email_deferred','support_ticket'
            )),
            title TEXT NOT NULL,
            body TEXT NOT NULL,
            project_id TEXT,
            metadata_json TEXT DEFAULT '{}',
            created_at TEXT NOT NULL DEFAULT (datetime('now')),
            delivered_at TEXT,
            digest_batch_id TEXT
        );
        CREATE INDEX IF NOT EXISTS idx_digest_queue_pending
            ON digest_queue(delivered_at, category);
    """)


def _migrate_v36(conn: sqlite3.Connection) -> None:
    """v36: Recreate support_tickets with outreach category.

    SQLite does not support ALTER TABLE ... MODIFY CONSTRAINT, so we recreate
    the table with the expanded CHECK constraint and copy existing rows.
    Uses explicit column list and proper transaction to avoid executescript() pitfalls.
    """
    _COLS = (
        "ticket_id,title,category,severity,status,source_type,"
        "related_project_id,counterparty,reporter_email,reporter_contact_id,"
        "description,root_cause,resolution,assigned_team,escalated_to,"
        "reported_at,resolved_at,closed_at,sla_target_hours,resolution_pattern_id,"
        "created_at,updated_at,thread_anchor,reply_draft_text,draft_generated_at,ai_summary_json"
    )
    conn.execute("PRAGMA foreign_keys = OFF")
    try:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS support_tickets_v36 (
                ticket_id TEXT PRIMARY KEY,
                title TEXT NOT NULL,
                category TEXT NOT NULL CHECK (category IN (
                    'fix_session','network','trade','post_trade',
                    'cert','config','access','monitoring','outreach','other'
                )),
                severity TEXT NOT NULL DEFAULT 'medium' CHECK (severity IN (
                    'critical','high','medium','low'
                )),
                status TEXT NOT NULL DEFAULT 'open' CHECK (status IN (
                    'open','investigating','waiting_external','waiting_internal',
                    'resolved','closed'
                )),
                source_type TEXT NOT NULL DEFAULT 'email',
                related_project_id TEXT,
                counterparty TEXT,
                reporter_email TEXT,
                reporter_contact_id TEXT,
                description TEXT,
                root_cause TEXT,
                resolution TEXT,
                assigned_team TEXT,
                escalated_to TEXT,
                reported_at TEXT,
                resolved_at TEXT,
                closed_at TEXT,
                sla_target_hours REAL,
                resolution_pattern_id TEXT,
                created_at TEXT,
                updated_at TEXT,
                thread_anchor TEXT,
                reply_draft_text TEXT,
                draft_generated_at TEXT,
                ai_summary_json TEXT
            )
        """)
        conn.execute(
            f"INSERT OR IGNORE INTO support_tickets_v36 ({_COLS}) "
            f"SELECT {_COLS} FROM support_tickets"
        )
        conn.execute("DROP TABLE IF EXISTS support_tickets")
        conn.execute("ALTER TABLE support_tickets_v36 RENAME TO support_tickets")
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_support_tickets_status ON support_tickets(status)"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_support_tickets_project ON support_tickets(related_project_id)"
        )
    finally:
        conn.execute("PRAGMA foreign_keys = ON")


def _ensure_col(conn: sqlite3.Connection, table: str, col: str, typedef: str) -> None:
    """Add column to table if it doesn't exist (idempotent)."""
    existing = {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}
    if col not in existing:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {col} {typedef}")
        logger.warning("reconcile: added %s.%s", table, col)


def _migrate_v37(conn: sqlite3.Connection) -> None:
    """v37: Introduce work_items supertype table. Backfill from projects and support_tickets."""

    # 1. Create work_items table + indexes + trigger (idempotent)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS work_items (
            work_item_id  TEXT PRIMARY KEY,
            work_item_type TEXT NOT NULL CHECK (
                work_item_type IN ('project','support_ticket','outreach','standalone')
            ),
            title         TEXT NOT NULL,
            status        TEXT NOT NULL DEFAULT 'open' CHECK (
                status IN ('open','resolved','closed')
            ),
            priority      TEXT DEFAULT 'medium' CHECK (
                priority IN ('critical','high','medium','low')
            ),
            entity_table  TEXT CHECK (
                entity_table IN ('projects','support_tickets') OR entity_table IS NULL
            ),
            entity_id     TEXT,
            created_at    TEXT NOT NULL DEFAULT (datetime('now')),
            updated_at    TEXT NOT NULL DEFAULT (datetime('now'))
        )
    """)
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_work_items_status ON work_items(status)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_work_items_type ON work_items(work_item_type)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_work_items_entity ON work_items(entity_table, entity_id)"
    )
    conn.executescript("""
        CREATE TRIGGER IF NOT EXISTS trg_work_items_updated_at
        AFTER UPDATE ON work_items
        BEGIN
            UPDATE work_items SET updated_at = datetime('now')
            WHERE work_item_id = NEW.work_item_id;
        END;
    """)

    # 2. Add work_item_id columns to entity tables (idempotent via _ensure_col)
    _ensure_col(conn, "projects",        "work_item_id", "TEXT")
    _ensure_col(conn, "support_tickets", "work_item_id", "TEXT")
    _ensure_col(conn, "drafts",          "work_item_id", "TEXT")

    # 3. Assert no draft has BOTH project_id AND ticket_id set
    bad_drafts = conn.execute(
        "SELECT COUNT(*) FROM drafts WHERE project_id IS NOT NULL AND ticket_id IS NOT NULL"
    ).fetchone()[0]
    if bad_drafts:
        raise RuntimeError(
            f"_migrate_v37: {bad_drafts} drafts have both project_id and ticket_id set"
        )

    # 4. Backfill projects → work_items (skip _system placeholder)
    conn.execute("""
        INSERT OR IGNORE INTO work_items
            (work_item_id, work_item_type, title, status, entity_table, entity_id, created_at)
        SELECT
            'wi_proj_' || project_id,
            'project',
            COALESCE(client_name, project_id),
            CASE project_status
                WHEN 'Completed' THEN 'closed'
                ELSE 'open'
            END,
            'projects',
            project_id,
            created_at
        FROM projects
        WHERE project_id != '_system'
    """)
    conn.execute("""
        UPDATE projects SET work_item_id = 'wi_proj_' || project_id
        WHERE work_item_id IS NULL AND project_id != '_system'
    """)

    # 5. Backfill support_tickets → work_items
    conn.execute("""
        INSERT OR IGNORE INTO work_items
            (work_item_id, work_item_type, title, status, entity_table, entity_id, created_at)
        SELECT
            'wi_tick_' || ticket_id,
            CASE category WHEN 'outreach' THEN 'outreach' ELSE 'support_ticket' END,
            title,
            CASE status
                WHEN 'closed'    THEN 'closed'
                WHEN 'resolved'  THEN 'closed'
                ELSE 'open'
            END,
            'support_tickets',
            ticket_id,
            created_at
        FROM support_tickets
    """)
    conn.execute("""
        UPDATE support_tickets SET work_item_id = 'wi_tick_' || ticket_id
        WHERE work_item_id IS NULL
    """)

    # 6. Link existing drafts via their project_id or ticket_id back-references
    conn.execute("""
        UPDATE drafts SET work_item_id = 'wi_proj_' || project_id
        WHERE project_id IS NOT NULL AND work_item_id IS NULL
    """)
    conn.execute("""
        UPDATE drafts SET work_item_id = 'wi_tick_' || ticket_id
        WHERE ticket_id IS NOT NULL AND work_item_id IS NULL
    """)

    conn.commit()


def _migrate_v38(conn: sqlite3.Connection) -> None:
    """v38: Add requested_docs_json column to inbound_replies."""
    try:
        conn.execute(
            "ALTER TABLE inbound_replies ADD COLUMN requested_docs_json TEXT"
        )
    except Exception:
        pass  # column already exists
    conn.commit()


def _migrate_v39(conn: sqlite3.Connection) -> None:
    """v39: Create internal_actions table for tracking internal team tasks."""
    conn.execute("""
        CREATE TABLE IF NOT EXISTS internal_actions (
            action_id    TEXT PRIMARY KEY,
            project_id   TEXT REFERENCES projects(project_id),
            owner_team   TEXT NOT NULL CHECK (owner_team IN (
                             'IT Network', 'Compliance', 'Demo Provisioning Support',
                             'Demo Connectivity Acceptance Support', 'Demo IT Security',
                             'Demo Trading Ops', 'Demo Post-Trade')),
            title        TEXT NOT NULL,
            deadline     TEXT,
            status       TEXT NOT NULL DEFAULT 'open'
                             CHECK (status IN ('open', 'done', 'cancelled')),
            created_at   TEXT NOT NULL DEFAULT (datetime('now')),
            completed_at TEXT
        )
    """)
    conn.commit()


def _migrate_v40(conn: sqlite3.Connection) -> None:
    """v40: Add domain + usage_count to email_knowledge_log; create wiki_usage_log."""
    # Add domain column to email_knowledge_log (idempotent)
    ekl_cols = {r[1] for r in conn.execute("PRAGMA table_info(email_knowledge_log)")}
    for col, typedef in [
        ("domain", "TEXT DEFAULT 'general'"),
        ("usage_count", "INTEGER DEFAULT 0"),
    ]:
        if col not in ekl_cols:
            conn.execute(f"ALTER TABLE email_knowledge_log ADD COLUMN {col} {typedef}")

    # Add domain index (idempotent via IF NOT EXISTS)
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_email_knowledge_log_domain "
        "ON email_knowledge_log(domain)"
    )

    # Create wiki_usage_log table (idempotent via IF NOT EXISTS)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS wiki_usage_log (
            id        INTEGER PRIMARY KEY AUTOINCREMENT,
            wiki_path TEXT NOT NULL,
            used_at   TEXT NOT NULL DEFAULT (datetime('now'))
        )
    """)
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_wiki_usage_log_path ON wiki_usage_log(wiki_path)"
    )
    conn.commit()


def _migrate_v41(conn: sqlite3.Connection) -> None:
    """v41: Add status column to email_knowledge_log for draft/active/archived lifecycle."""
    ekl_cols = {r[1] for r in conn.execute("PRAGMA table_info(email_knowledge_log)")}
    if "status" not in ekl_cols:
        conn.execute(
            "ALTER TABLE email_knowledge_log ADD COLUMN status TEXT NOT NULL DEFAULT 'active'"
        )
    conn.commit()


def _migrate_v42(conn: sqlite3.Connection) -> None:
    """v42: Add thinking_window_enabled column to operator_state (Telegram /thinking debug toggle)."""
    existing_cols = {row[1] for row in conn.execute("PRAGMA table_info(operator_state)")}
    if "thinking_window_enabled" not in existing_cols:
        conn.execute(
            "ALTER TABLE operator_state ADD COLUMN thinking_window_enabled INTEGER NOT NULL DEFAULT 0"
        )
    conn.commit()


def _migrate_v43(conn: sqlite3.Connection) -> None:
    """v43: Draft status transition audit trail."""
    conn.execute(
        """CREATE TABLE IF NOT EXISTS draft_status_history (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            draft_id TEXT NOT NULL REFERENCES drafts(draft_id),
            from_status TEXT,
            to_status TEXT NOT NULL,
            actor TEXT NOT NULL DEFAULT 'system',
            surface TEXT,
            changed_at TEXT NOT NULL DEFAULT (datetime('now'))
        )"""
    )
    conn.execute(
        """CREATE INDEX IF NOT EXISTS idx_draft_status_history_draft
           ON draft_status_history(draft_id, changed_at DESC)"""
    )
    conn.commit()


def _migrate_v46(conn: sqlite3.Connection) -> None:
    """v46: Unified operator memory store + thread artifacts."""
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS operator_memory (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            operator_id TEXT NOT NULL,
            scope TEXT NOT NULL CHECK (scope IN ('global', 'project', 'chat', 'counterparty')),
            scope_id TEXT NOT NULL DEFAULT '*',
            memory_type TEXT NOT NULL CHECK (
                memory_type IN ('semantic', 'procedural', 'forbidden', 'episodic_snippet')
            ),
            memory_key TEXT NOT NULL,
            memory_value TEXT NOT NULL,
            source TEXT NOT NULL DEFAULT 'operator'
                CHECK (source IN ('operator', 'memory_flush', 'dashboard', 'migration', 'eval')),
            active INTEGER NOT NULL DEFAULT 1,
            created_at TEXT NOT NULL DEFAULT (datetime('now')),
            updated_at TEXT NOT NULL DEFAULT (datetime('now')),
            UNIQUE (operator_id, scope, scope_id, memory_type, memory_key)
        );
        CREATE INDEX IF NOT EXISTS idx_operator_memory_lookup
            ON operator_memory (operator_id, scope, scope_id, memory_type, active);

        CREATE TABLE IF NOT EXISTS thread_artifacts (
            artifact_id TEXT PRIMARY KEY,
            operator_id TEXT NOT NULL,
            thread_id TEXT NOT NULL,
            tool_name TEXT NOT NULL,
            project_id TEXT,
            content_json TEXT NOT NULL,
            char_count INTEGER NOT NULL,
            created_at TEXT NOT NULL DEFAULT (datetime('now'))
        );
        CREATE INDEX IF NOT EXISTS idx_thread_artifacts_thread
            ON thread_artifacts (operator_id, thread_id, created_at DESC);
        """
    )
    conn.commit()


def _migrate_v47(conn: sqlite3.Connection) -> None:
    """v47: Normalize legacy learned_rules.status='active' (not injectable)."""
    from src.repositories.learned_rules_repo import normalize_legacy_active_statuses

    n = normalize_legacy_active_statuses(conn)
    logger.info("Migration v47: normalized %d learned_rules with status=active", n)
    conn.commit()


def _migrate_v48(conn: sqlite3.Connection) -> None:
    """v48: Specialist sessions for delegate resume_from continuation."""
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS specialist_sessions (
            session_id TEXT PRIMARY KEY,
            operator_id TEXT NOT NULL,
            thread_id TEXT NOT NULL,
            skill_id TEXT NOT NULL,
            specialist_type TEXT NOT NULL,
            project_id TEXT,
            parent_session_id TEXT,
            messages_json TEXT NOT NULL,
            tool_history_json TEXT,
            status TEXT NOT NULL DEFAULT 'completed'
                CHECK (status IN ('completed', 'exhausted', 'failed')),
            created_at TEXT NOT NULL DEFAULT (datetime('now')),
            expires_at TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_specialist_sessions_thread
            ON specialist_sessions (operator_id, thread_id, created_at DESC);
        CREATE INDEX IF NOT EXISTS idx_specialist_sessions_expires
            ON specialist_sessions (expires_at);
        """
    )
    conn.commit()


def _migrate_v49(conn: sqlite3.Connection) -> None:
    """Close/resolve work_items whose linked project/ticket is already terminal."""
    conn.execute("""
        UPDATE work_items
        SET status = 'closed'
        WHERE entity_table = 'projects'
          AND entity_id IN (
            SELECT project_id FROM projects
            WHERE project_status IN ('Completed','Archived','Cancelled')
          )
          AND status = 'open'
    """)
    conn.execute("""
        UPDATE work_items
        SET status = 'resolved'
        WHERE entity_table = 'support_tickets'
          AND entity_id IN (
            SELECT ticket_id FROM support_tickets WHERE status = 'resolved'
          )
          AND status != 'resolved'
    """)
    conn.execute("""
        UPDATE work_items
        SET status = 'closed'
        WHERE entity_table = 'support_tickets'
          AND entity_id IN (
            SELECT ticket_id FROM support_tickets WHERE status = 'closed'
          )
          AND status != 'closed'
    """)
    conn.commit()


def _migrate_v50(conn: sqlite3.Connection) -> None:
    """Party graph tables + contact_suggestions columns for global identity."""
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS project_parties (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            project_id TEXT NOT NULL REFERENCES projects(project_id),
            org_id TEXT NOT NULL REFERENCES organisations(org_id),
            party_role TEXT NOT NULL DEFAULT 'other'
                CHECK (party_role IN (
                    'client','vendor','regulator','internal','counterparty','other'
                )),
            is_primary INTEGER NOT NULL DEFAULT 0,
            notes TEXT,
            created_at TEXT NOT NULL DEFAULT (datetime('now')),
            updated_at TEXT NOT NULL DEFAULT (datetime('now')),
            UNIQUE(project_id, org_id, party_role)
        );
        CREATE INDEX IF NOT EXISTS idx_project_parties_project
            ON project_parties(project_id);
        CREATE INDEX IF NOT EXISTS idx_project_parties_org
            ON project_parties(org_id);

        CREATE TABLE IF NOT EXISTS contact_project_links (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            contact_id TEXT NOT NULL REFERENCES contacts(contact_id),
            project_id TEXT NOT NULL REFERENCES projects(project_id),
            role_on_project TEXT DEFAULT 'other'
                CHECK (role_on_project IN (
                    'pm','developer','ops','cert','compliance','other'
                )),
            is_primary INTEGER NOT NULL DEFAULT 0,
            source TEXT NOT NULL DEFAULT 'identity_confirm',
            created_at TEXT NOT NULL DEFAULT (datetime('now')),
            UNIQUE(contact_id, project_id)
        );
        CREATE INDEX IF NOT EXISTS idx_contact_project_links_project
            ON contact_project_links(project_id);
        CREATE INDEX IF NOT EXISTS idx_contact_project_links_contact
            ON contact_project_links(contact_id);
        """
    )
    # contact_suggestions extras (idempotent)
    cols = {row[1] for row in conn.execute("PRAGMA table_info(contact_suggestions)")}
    if "org_type_guess" not in cols:
        conn.execute(
            "ALTER TABLE contact_suggestions ADD COLUMN org_type_guess TEXT DEFAULT 'unknown'"
        )
    if "last_pushed_at" not in cols:
        conn.execute(
            "ALTER TABLE contact_suggestions ADD COLUMN last_pushed_at TEXT"
        )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_contact_suggestions_email_status "
        "ON contact_suggestions(sender_email, status)"
    )

    # Backfill contact_project_links from cross_project_history JSON
    rows = conn.execute(
        "SELECT contact_id, cross_project_history FROM contacts "
        "WHERE cross_project_history IS NOT NULL AND cross_project_history != '[]'"
    ).fetchall()
    import json as _json
    for row in rows:
        try:
            history = _json.loads(row["cross_project_history"] or "[]")
        except Exception:
            history = []
        if not isinstance(history, list):
            continue
        for pid in history:
            pid = str(pid).strip()
            if not pid:
                continue
            exists = conn.execute(
                "SELECT 1 FROM projects WHERE project_id = ?", (pid,)
            ).fetchone()
            if not exists:
                continue
            conn.execute(
                """INSERT OR IGNORE INTO contact_project_links
                   (contact_id, project_id, role_on_project, source)
                   VALUES (?, ?, 'other', 'history_backfill')""",
                (row["contact_id"], pid),
            )
    conn.commit()


def _migrate_v51(conn: sqlite3.Connection) -> None:
    """v51: Link support tickets to party-graph organisations."""
    cols = {row[1] for row in conn.execute("PRAGMA table_info(support_tickets)")}
    if "counterparty_org_id" not in cols:
        conn.execute(
            "ALTER TABLE support_tickets ADD COLUMN counterparty_org_id TEXT "
            "REFERENCES organisations(org_id)"
        )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_support_tickets_counterparty_org "
        "ON support_tickets(counterparty_org_id)"
    )
    # Backfill from free-text counterparty + reporter_email domains
    from src.repositories.party_repo import resolve_counterparty_org_id

    rows = conn.execute(
        """SELECT ticket_id, counterparty, reporter_email, counterparty_org_id
           FROM support_tickets"""
    ).fetchall()
    for row in rows:
        if row["counterparty_org_id"]:
            continue
        org_id = resolve_counterparty_org_id(
            conn,
            counterparty=row["counterparty"],
            reporter_email=row["reporter_email"],
        )
        if org_id:
            conn.execute(
                "UPDATE support_tickets SET counterparty_org_id = ? WHERE ticket_id = ?",
                (org_id, row["ticket_id"]),
            )
    conn.commit()


def _migrate_v60(conn: sqlite3.Connection) -> None:
    """v60: Add immutable, evidence-backed meeting-note ingestions."""
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS meeting_ingestions (
            ingestion_id TEXT PRIMARY KEY,
            operator_id TEXT NOT NULL DEFAULT 'default_operator',
            calendar_event_id TEXT NOT NULL REFERENCES calendar_events(calendar_event_id),
            source_hash TEXT NOT NULL,
            source_text TEXT NOT NULL,
            extracted_json TEXT NOT NULL DEFAULT '{}',
            work_item_ids_json TEXT NOT NULL DEFAULT '[]',
            created_at TEXT NOT NULL,
            UNIQUE(operator_id, calendar_event_id, source_hash)
        );
        CREATE INDEX IF NOT EXISTS idx_meeting_ingestions_event
            ON meeting_ingestions(operator_id, calendar_event_id, created_at DESC);
        """
    )
    conn.commit()


def _migrate_v61(conn: sqlite3.Connection) -> None:
    """v61: Add owned, expiring, optimistic pending operations."""
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS pending_operations (
            operation_id TEXT PRIMARY KEY,
            operation_type TEXT NOT NULL,
            operator_id TEXT NOT NULL DEFAULT 'default_operator',
            chat_id TEXT NOT NULL,
            project_id TEXT REFERENCES projects(project_id),
            payload_json TEXT NOT NULL DEFAULT '{}',
            base_version TEXT,
            status TEXT NOT NULL DEFAULT 'pending' CHECK (
                status IN ('pending','confirmed','cancelled','expired','superseded','failed')
            ),
            created_at TEXT NOT NULL,
            expires_at TEXT NOT NULL,
            resolved_at TEXT,
            resolution TEXT
        );
        CREATE INDEX IF NOT EXISTS idx_pending_operations_scope
            ON pending_operations(operator_id, chat_id, status, created_at DESC);
        CREATE INDEX IF NOT EXISTS idx_pending_operations_project
            ON pending_operations(project_id, operation_type, status);
        CREATE INDEX IF NOT EXISTS idx_pending_operations_expiry
            ON pending_operations(expires_at, status);
        """
    )
    conn.commit()


def _migrate_v62(conn: sqlite3.Connection) -> None:
    """v62: Add a durable sending state before external email delivery."""
    sql = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='drafts'"
    ).fetchone()
    if sql and "'sending'" in (sql[0] or ""):
        return
    conn.executescript(
        """
        PRAGMA foreign_keys = OFF;
        CREATE TABLE drafts_v62 (
            draft_id TEXT PRIMARY KEY,
            project_id TEXT REFERENCES projects(project_id),
            task_type TEXT,
            draft_version INTEGER DEFAULT 1,
            current_draft_text TEXT,
            draft_history TEXT DEFAULT '[]',
            status TEXT DEFAULT 'created'
                CHECK (status IN ('created','in_review','approved','sending','sent','discarded','send_failed')),
            to_email TEXT,
            cc TEXT,
            subject TEXT,
            context_packet_ref TEXT,
            ticket_id TEXT REFERENCES support_tickets(ticket_id),
            work_item_id TEXT REFERENCES work_items(work_item_id),
            pattern_used TEXT,
            sent_version TEXT,
            sent_at TEXT,
            scheduled_send_at TEXT,
            created_at TEXT NOT NULL DEFAULT (datetime('now'))
        );
        INSERT INTO drafts_v62 SELECT * FROM drafts;
        DROP TABLE drafts;
        ALTER TABLE drafts_v62 RENAME TO drafts;
        CREATE INDEX IF NOT EXISTS idx_drafts_project ON drafts(project_id, status);
        CREATE INDEX IF NOT EXISTS idx_drafts_ticket ON drafts(ticket_id);
        CREATE INDEX IF NOT EXISTS idx_drafts_work_item ON drafts(work_item_id);
        PRAGMA foreign_keys = ON;
        """
    )
    conn.commit()


def _migrate_v97(conn: sqlite3.Connection) -> None:
    """Repair v96 databases whose drafts CHECK constraint omitted ``sending``."""
    row = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='drafts'"
    ).fetchone()
    table_sql = (row[0] if row else "") or ""
    if not table_sql or "'sending'" in table_sql:
        return

    expected_columns = [
        "draft_id",
        "project_id",
        "task_type",
        "draft_version",
        "current_draft_text",
        "draft_history",
        "status",
        "to_email",
        "cc",
        "subject",
        "context_packet_ref",
        "ticket_id",
        "work_item_id",
        "pattern_used",
        "sent_version",
        "sent_at",
        "scheduled_send_at",
        "created_at",
    ]
    existing_columns = {row[1] for row in conn.execute("PRAGMA table_info(drafts)")}
    missing = sorted(set(expected_columns) - existing_columns)
    if missing:
        raise sqlite3.OperationalError(
            f"Cannot repair drafts status constraint; missing columns: {', '.join(missing)}"
        )

    column_list = ", ".join(expected_columns)
    conn.commit()
    conn.execute("PRAGMA foreign_keys = OFF")
    try:
        conn.execute("DROP TABLE IF EXISTS drafts_v97")
        conn.execute("BEGIN")
        conn.execute(
            """CREATE TABLE drafts_v97 (
                draft_id TEXT PRIMARY KEY,
                project_id TEXT REFERENCES projects(project_id),
                task_type TEXT,
                draft_version INTEGER DEFAULT 1,
                current_draft_text TEXT,
                draft_history TEXT DEFAULT '[]',
                status TEXT DEFAULT 'created'
                    CHECK (status IN ('created','in_review','approved','sending','sent','discarded','send_failed')),
                to_email TEXT,
                cc TEXT,
                subject TEXT,
                context_packet_ref TEXT,
                ticket_id TEXT REFERENCES support_tickets(ticket_id),
                work_item_id TEXT REFERENCES work_items(work_item_id),
                pattern_used TEXT,
                sent_version TEXT,
                sent_at TEXT,
                scheduled_send_at TEXT,
                created_at TEXT NOT NULL DEFAULT (datetime('now'))
            )"""
        )
        conn.execute(
            f"INSERT INTO drafts_v97 ({column_list}) SELECT {column_list} FROM drafts"
        )
        conn.execute("DROP TABLE drafts")
        conn.execute("ALTER TABLE drafts_v97 RENAME TO drafts")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_drafts_project ON drafts(project_id, status)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_drafts_ticket ON drafts(ticket_id)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_drafts_work_item ON drafts(work_item_id)")
        conn.execute("COMMIT")
    except Exception:
        try:
            conn.execute("ROLLBACK")
        except sqlite3.OperationalError:
            pass
        raise
    finally:
        conn.execute("PRAGMA foreign_keys = ON")
    conn.commit()


def _migrate_v98(conn: sqlite3.Connection) -> None:
    """Add source timestamps and structured recipient evidence to processed emails."""
    columns = {row[1] for row in conn.execute("PRAGMA table_info(processed_emails)")}
    for col, typedef in [
        ("received_at", "TEXT"),
        ("to_recipients_json", "TEXT NOT NULL DEFAULT '[]'"),
        ("cc_recipients_json", "TEXT NOT NULL DEFAULT '[]'"),
        ("bcc_recipients_json", "TEXT NOT NULL DEFAULT '[]'"),
    ]:
        if col not in columns:
            conn.execute(f"ALTER TABLE processed_emails ADD COLUMN {col} {typedef}")
    conn.execute(
        """UPDATE processed_emails
           SET received_at=processed_at
           WHERE received_at IS NULL OR trim(received_at)=''"""
    )
    conn.execute(
        """CREATE INDEX IF NOT EXISTS idx_processed_emails_project_received
           ON processed_emails(project_id, received_at DESC)"""
    )
    conn.commit()


def _migrate_v99(conn: sqlite3.Connection) -> None:
    """Create and idempotently backfill the secure draft workspace."""
    from src.email.draft_workspace import backfill_draft_workspaces
    from src.email.workspace_schema import ensure_draft_workspace_schema

    ensure_draft_workspace_schema(conn)
    backfill_draft_workspaces(conn)
    conn.commit()


def _backfill_chat_turn_ids(
    conn: sqlite3.Connection,
    *,
    table: str,
    resource_column: str,
    legacy_prefix: str,
) -> None:
    """Pair each legacy user message with its immediately following assistant reply."""
    rows = conn.execute(
        f"SELECT id, {resource_column} AS resource_id, owner_user_id, role, turn_id "
        f"FROM {table} "
        f"ORDER BY {resource_column}, owner_user_id, created_at, id"
    ).fetchall()

    index = 0
    while index < len(rows):
        message_id, resource_id, owner_user_id, role, raw_turn_id = rows[index]
        message_id = int(message_id)
        turn_id = str(raw_turn_id or "").strip()
        if role == "user":
            active_turn_id = turn_id or f"{legacy_prefix}-{message_id}"
            if not turn_id:
                conn.execute(
                    f"UPDATE {table} SET turn_id=? WHERE id=? AND turn_id IS NULL",
                    (active_turn_id, message_id),
                )
            if index + 1 < len(rows):
                (
                    next_message_id,
                    next_resource_id,
                    next_owner_user_id,
                    next_role,
                    next_raw_turn_id,
                ) = rows[index + 1]
                same_conversation = (
                    next_resource_id == resource_id
                    and next_owner_user_id == owner_user_id
                )
                if (
                    same_conversation
                    and next_role == "assistant"
                    and not str(next_raw_turn_id or "").strip()
                ):
                    conn.execute(
                        f"UPDATE {table} SET turn_id=? WHERE id=? AND turn_id IS NULL",
                        (active_turn_id, int(next_message_id)),
                    )
                    index += 2
                    continue
        elif not turn_id:
            conn.execute(
                f"UPDATE {table} SET turn_id=? WHERE id=? AND turn_id IS NULL",
                (f"{legacy_prefix}-{message_id}", message_id),
            )
        index += 1


def _migrate_v100(conn: sqlite3.Connection) -> None:
    """Add stable turn identities to project and support web chat history."""
    tables = {
        row[0]
        for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()
    }
    chat_tables = (
        (
            "project_chat_messages",
            "project_id",
            "legacy-project",
            "idx_project_chat_turn",
        ),
        (
            "support_chat_messages",
            "ticket_id",
            "legacy-support",
            "idx_support_chat_turn",
        ),
    )
    for table, resource_column, legacy_prefix, index_name in chat_tables:
        if table not in tables:
            continue
        columns = {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}
        if "turn_id" not in columns:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN turn_id TEXT")
        _backfill_chat_turn_ids(
            conn,
            table=table,
            resource_column=resource_column,
            legacy_prefix=legacy_prefix,
        )
        conn.execute(
            f"CREATE INDEX IF NOT EXISTS {index_name} "
            f"ON {table}({resource_column}, owner_user_id, turn_id, created_at)"
        )
    conn.commit()



def _migrate_v101(conn: sqlite3.Connection) -> None:
    """Create AI Workspace storage and idempotently import project web chats."""
    import hashlib
    import json

    from src.assistant.schema import ensure_assistant_schema

    ensure_assistant_schema(conn)
    tables = {
        row[0]
        for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()
    }
    if "project_chat_messages" not in tables:
        conn.commit()
        return

    groups = conn.execute(
        """SELECT pcm.owner_user_id, pcm.project_id,
                  COALESCE(NULLIF(trim(p.client_name), ''), pcm.project_id) AS project_title,
                  MIN(pcm.created_at) AS first_created,
                  MAX(pcm.created_at) AS last_created
           FROM project_chat_messages pcm
           JOIN projects p ON p.project_id = pcm.project_id
           WHERE pcm.project_id != '_system'
           GROUP BY pcm.owner_user_id, pcm.project_id"""
    ).fetchall()
    for group in groups:
        owner_user_id = str(group[0] or "default_operator")
        project_id = str(group[1])
        digest = hashlib.sha256(
            f"{owner_user_id}\0{project_id}".encode("utf-8")
        ).hexdigest()[:24]
        thread_id = f"legacy-project-{digest}"
        title = f"{group[2]} workspace"
        conn.execute(
            """INSERT OR IGNORE INTO assistant_threads (
                   thread_id, owner_user_id, title, scope_mode, web_enabled,
                   created_at, updated_at
               ) VALUES (?, ?, ?, 'projects', 0, ?, ?)""",
            (thread_id, owner_user_id, title, group[3], group[4]),
        )
        conn.execute(
            """INSERT OR IGNORE INTO assistant_thread_projects (
                   thread_id, project_id, position, added_at
               ) VALUES (?, ?, 0, ?)""",
            (thread_id, project_id, group[3]),
        )

        rows = conn.execute(
            """SELECT id, turn_id, role, content, created_at
               FROM project_chat_messages
               WHERE owner_user_id=? AND project_id=?
               ORDER BY created_at, id""",
            (owner_user_id, project_id),
        ).fetchall()
        turn_ids: dict[str, str] = {}
        for row in rows:
            legacy_turn = str(row[1] or f"legacy-project-{row[0]}")
            workspace_turn_id = turn_ids.get(legacy_turn)
            if workspace_turn_id is None:
                turn_digest = hashlib.sha256(
                    f"{thread_id}\0{legacy_turn}".encode("utf-8")
                ).hexdigest()[:24]
                workspace_turn_id = f"legacy-turn-{turn_digest}"
                turn_ids[legacy_turn] = workspace_turn_id
                conn.execute(
                    """INSERT OR IGNORE INTO assistant_turns (
                           turn_id, thread_id, client_turn_id, command, user_text,
                           scope_mode_snapshot, project_ids_snapshot_json,
                           web_enabled_snapshot, execution_status, created_at,
                           started_at, completed_at
                       ) VALUES (?, ?, ?, NULL, ?, 'projects', ?, 0, 'completed', ?, ?, ?)""",
                    (
                        workspace_turn_id,
                        thread_id,
                        f"legacy:{legacy_turn}",
                        row[3] if row[2] == "user" else "Imported project chat turn",
                        json.dumps([project_id]),
                        row[4],
                        row[4],
                        row[4],
                    ),
                )
            message_id = f"legacy-project-message-{row[0]}"
            parts = [{"type": "text", "text": row[3]}]
            conn.execute(
                """INSERT OR IGNORE INTO assistant_messages (
                       message_id, thread_id, turn_id, role, content, parts_json,
                       legacy_source, legacy_source_id, created_at
                   ) VALUES (?, ?, ?, ?, ?, ?, 'project_chat_messages', ?, ?)""",
                (
                    message_id,
                    thread_id,
                    workspace_turn_id,
                    row[2],
                    row[3],
                    json.dumps(parts, ensure_ascii=False),
                    str(row[0]),
                    row[4],
                ),
            )
    conn.commit()

def _migrate_v63(conn: sqlite3.Connection) -> None:
    """v63: Add durable learning queue and retryable email-mining state."""
    existing = {row[1] for row in conn.execute("PRAGMA table_info(processed_emails)")}
    for col, typedef in [
        ("knowledge_mining_status", "TEXT NOT NULL DEFAULT 'pending'"),
        ("knowledge_mining_attempts", "INTEGER NOT NULL DEFAULT 0"),
        ("knowledge_mining_error", "TEXT"),
        ("knowledge_mining_lease_owner", "TEXT"),
        ("knowledge_mining_lease_expires_at", "TEXT"),
        ("knowledge_mined_at", "TEXT"),
    ]:
        if col not in existing:
            conn.execute(f"ALTER TABLE processed_emails ADD COLUMN {col} {typedef}")
    conn.execute(
        """UPDATE processed_emails
           SET knowledge_mining_status = CASE
               WHEN knowledge_mined = 1 THEN 'mined'
               ELSE COALESCE(NULLIF(knowledge_mining_status, ''), 'pending')
           END"""
    )
    outbox_cols = {row[1] for row in conn.execute("PRAGMA table_info(learning_outbox)")}
    if not outbox_cols or "job_id" in outbox_cols:
        conn.executescript(_LEARNING_OUTBOX_DDL)
    conn.execute(
        """CREATE INDEX IF NOT EXISTS idx_processed_emails_mining_due
           ON processed_emails(
               knowledge_mined, knowledge_mining_status,
               knowledge_mining_lease_expires_at, processed_at
           )"""
    )
    conn.commit()


def _migrate_v65(conn: sqlite3.Connection) -> None:
    """v65: Create the immutable, capture-only learning observation ledger."""
    from src.db.schema import apply_schema
    apply_schema(conn)


def _migrate_v66(conn: sqlite3.Connection) -> None:
    """v66: Replace the legacy conversation queue with observation-bound jobs."""
    cols = {row[1] for row in conn.execute("PRAGMA table_info(learning_outbox)")}
    if cols and "job_id" not in cols:
        conn.execute("ALTER TABLE learning_outbox RENAME TO learning_outbox_legacy_v63")
    conn.executescript(_LEARNING_OUTBOX_DDL)
    if conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='learning_outbox_legacy_v63'").fetchone():
        rows = conn.execute("SELECT * FROM learning_outbox_legacy_v63 ORDER BY created_at, outbox_id").fetchall()
        from src.learning.ingest.base import capture_observation
        from src.learning.ingest.contracts import ObservationSourceType, TrustTier
        from src.learning.ingest.conversation import capture_conversation_flush
        from src.learning.jobs.outbox_repo import enqueue_job
        import json
        for row in rows:
            item = dict(row)
            messages = json.loads(item.get("messages_json") or "[]")
            if not isinstance(messages, list):
                raise ValueError(f"legacy learning outbox {item['outbox_id']} messages must be a list")
            source_id = f"legacy-outbox:{item['outbox_id']}"
            if messages:
                observation = capture_conversation_flush(
                    conn,
                    source_id=source_id,
                    operator_id="default_operator",
                    messages=messages,
                )
            else:
                observation = capture_observation(
                    conn,
                    source_type=ObservationSourceType.MIGRATION,
                    source_id=source_id,
                    operator_id="default_operator",
                    text="Legacy conversation payload was empty; no conversational content was available.",
                    raw_payload={
                        "legacy_outbox_id": item["outbox_id"],
                        "legacy_surface": item.get("surface"),
                        "legacy_conversation_id": item.get("conversation_id"),
                        "legacy_status": item.get("status"),
                        "message_count": 0,
                        "roles": [],
                    },
                    trust_tier=TrustTier.SYSTEM,
                    artifact_type="legacy_record",
                    artifact_id=item["outbox_id"],
                )
            job_id = enqueue_job(
                conn,
                observation_id=observation.observation_id,
                job_type="conversation_extraction",
                extractor_version="legacy-v1",
                payload={
                    "chat_id": item.get("notify_chat_id"),
                    "round_count": item.get("round_count", 0),
                    "tool_call_count": item.get("tool_call_count", 0),
                },
            )
            if item.get("status") == "completed":
                conn.execute(
                    "UPDATE learning_outbox SET state='completed', completed_at=COALESCE(?, datetime('now')), result_json=? WHERE job_id=?",
                    (item.get("completed_at"), item.get("result_json"), job_id),
                )
        converted = conn.execute("SELECT COUNT(*) FROM learning_outbox WHERE extractor_version='legacy-v1'").fetchone()[0]
        if converted != len(rows):
            raise RuntimeError(f"legacy learning outbox conversion mismatch: {converted}/{len(rows)}")
        conn.execute("DROP TABLE learning_outbox_legacy_v63")
    _repair_learning_job_effects_fk(conn)
    conn.commit()


def _migrate_v67(conn: sqlite3.Connection) -> None:
    """v67: Add immutable-versioned, proposal-only candidate persistence."""
    conn.executescript(_MEMORY_CANDIDATES_DDL)
    conn.commit()


def _migrate_v68(conn: sqlite3.Connection) -> None:
    """v68: Add governance metadata without rebuilding the v67 candidate table."""
    cols = {row[1] for row in conn.execute("PRAGMA table_info(memory_candidates)")}
    if cols and "revision_of_validation_id" not in cols:
        conn.execute("ALTER TABLE memory_candidates ADD COLUMN revision_of_validation_id TEXT")
    conn.executescript(_CANDIDATE_GOVERNANCE_DDL)
    conn.commit()


def _migrate_v69(conn: sqlite3.Connection) -> None:
    """v69: Add deterministic candidate keys, evidence unions, and conflict records."""
    conn.executescript(_CANDIDATE_CONFLICT_DDL)
    conn.commit()


def _migrate_v70(conn: sqlite3.Connection) -> None:
    """v70: Add typed semantic state and append-only conflict lifecycle history."""
    semantic_cols = {row[1] for row in conn.execute("PRAGMA table_info(candidate_semantic_keys)")}
    if semantic_cols and "match_type" not in semantic_cols:
        conn.execute("ALTER TABLE candidate_semantic_keys ADD COLUMN match_type TEXT NOT NULL DEFAULT 'unique'")
    event_cols = {row[1] for row in conn.execute("PRAGMA table_info(memory_conflict_events)")}
    if event_cols and "event_type" not in event_cols:
        conn.execute("ALTER TABLE memory_conflict_events ADD COLUMN event_type TEXT NOT NULL DEFAULT 'status-changed'")
    conn.executescript(_CANDIDATE_CONFLICT_DDL)
    conn.execute("""INSERT OR IGNORE INTO memory_conflict_events
        (conflict_id, event_seq, status, risk_level, actor_id, superseded_by_candidate_id, superseded_by_version, event_type, created_at)
        SELECT conflict_id, 1, status, risk_level, 'migration-v70', superseded_by_candidate_id, superseded_by_version, 'status-changed', created_at FROM memory_conflicts""")
    conn.commit()


def _migrate_v71(conn: sqlite3.Connection) -> None:
    """v71: Add append-only deterministic candidate evaluation records."""
    from src.db.schema import SCHEMA_DDL
    start=SCHEMA_DDL.index("CREATE TABLE IF NOT EXISTS candidate_evaluations")
    end=SCHEMA_DDL.index("-- Internal actions (v39)")
    conn.executescript(SCHEMA_DDL[start:end]); conn.commit()






def _table_exists(conn, name: str) -> bool:
    return conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)).fetchone() is not None



_EXACT_LEDGER_TRIGGERS = {
    "trg_output_revisions_no_update", "trg_output_revisions_no_delete",
    "trg_memory_exposures_no_update", "trg_memory_exposures_no_delete",
    "trg_memory_exposures_revision_match", "trg_learning_outcomes_no_update",
    "trg_learning_outcomes_no_delete", "trg_learning_outcomes_exact_match",
}


def _required_not_null(conn, table: str, columns: set[str]) -> bool:
    info = {
        row[1]: bool(row[3]) or bool(row[5])
        for row in conn.execute(f"PRAGMA table_info({table})")
    }
    return all(info.get(column) for column in columns)


def _exact_ledgers_complete(conn: sqlite3.Connection) -> bool:
    if not all(_table_exists(conn, name) for name in (
        "output_revisions", "memory_exposures", "learning_outcomes",
    )):
        return False
    revision_fks = {(r[3], r[2]) for r in conn.execute("PRAGMA foreign_key_list(output_revisions)")}
    exposure_fks = {(r[3], r[2]) for r in conn.execute("PRAGMA foreign_key_list(memory_exposures)")}
    outcome_fks = {(r[3], r[2]) for r in conn.execute("PRAGMA foreign_key_list(learning_outcomes)")}
    triggers = {
        row[0] for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='trigger'"
        )
    }
    return (
        _required_not_null(conn, "output_revisions", {
            "output_revision_id", "operator_id", "surface", "artifact_type",
            "artifact_id", "revision", "content_hash", "created_at",
        })
        and _required_not_null(conn, "memory_exposures", {
            "exposure_id", "trace_id", "output_revision_id", "operator_id",
            "surface", "source_surface", "fact_id", "fact_version",
            "rendered_hash", "rank", "token_count", "created_at",
        })
        and _required_not_null(conn, "learning_outcomes", {
            "outcome_id", "idempotency_key", "exposure_id", "trace_id",
            "output_revision_id", "operator_id", "surface", "fact_id",
            "fact_version", "outcome_type", "outcome_value", "actor_id", "created_at",
        })
        and ("prior_output_revision_id", "output_revisions") in revision_fks
        and ("output_revision_id", "output_revisions") in exposure_fks
        and {("exposure_id", "memory_exposures"),
             ("output_revision_id", "output_revisions")} <= outcome_fks
        and _EXACT_LEDGER_TRIGGERS <= triggers
    )


def _migrate_v82(conn: sqlite3.Connection) -> None:
    """v82: Historical lifecycle baseline; v83 reconciles the final contract."""
    from src.learning.lifecycle_schema import ensure_lifecycle_schema
    ensure_lifecycle_schema(conn)


def _migrate_v83(conn: sqlite3.Connection) -> None:
    """v83: Reconcile typed policies, exact denylist, reviews, and rollback operations."""
    from src.learning.lifecycle_schema import ensure_lifecycle_schema
    ensure_lifecycle_schema(conn)


def _migrate_v84(conn: sqlite3.Connection) -> None:
    """v84: Preserve legacy lifecycle rows while adding governed validity/event ledgers."""
    from src.learning.lifecycle_schema import ensure_lifecycle_schema
    ensure_lifecycle_schema(conn)


def _migrate_v85(conn: sqlite3.Connection) -> None:
    """v85: Rebuild policy snapshots with horizons, hashes, evidence, and quarantine."""
    from src.learning.lifecycle_schema import ensure_lifecycle_schema
    ensure_lifecycle_schema(conn)


def _migrate_v86(conn: sqlite3.Connection) -> None:
    """v86: Repair partial WP11 migrations and recreate canonical table triggers."""
    from src.learning.lifecycle_schema import ensure_lifecycle_schema
    ensure_lifecycle_schema(conn)


def _migrate_v87(conn: sqlite3.Connection) -> None:
    """v87: Add idempotent governed Wiki publication records and ledgers."""
    from src.wiki.publication.schema import ensure_publication_schema
    ensure_publication_schema(conn)


def _migrate_v88(conn: sqlite3.Connection) -> None:
    """v88: Add durable publication claims, phases, and immutable bindings."""
    from src.wiki.publication.schema import ensure_publication_schema
    ensure_publication_schema(conn)


def _migrate_v89(conn: sqlite3.Connection) -> None:
    """v89: Persist immutable candidate merges and reason-bearing conflict events."""
    columns = {row[1] for row in conn.execute("PRAGMA table_info(memory_conflict_events)")}
    for name in ("reason", "action", "confirmation_id"):
        if name not in columns:
            conn.execute(f"ALTER TABLE memory_conflict_events ADD COLUMN {name} TEXT")
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS candidate_merge_decisions (
            decision_id TEXT PRIMARY KEY,
            idempotency_key TEXT NOT NULL UNIQUE,
            canonical_candidate_id TEXT NOT NULL,
            canonical_candidate_version INTEGER NOT NULL,
            merged_candidate_id TEXT NOT NULL,
            merged_candidate_version INTEGER NOT NULL,
            conflict_id TEXT REFERENCES memory_conflicts(conflict_id),
            actor_id TEXT NOT NULL CHECK(length(trim(actor_id)) > 0),
            reason TEXT NOT NULL CHECK(length(trim(reason)) > 0),
            created_at TEXT NOT NULL,
            FOREIGN KEY(canonical_candidate_id, canonical_candidate_version)
                REFERENCES memory_candidates(candidate_id, candidate_version),
            FOREIGN KEY(merged_candidate_id, merged_candidate_version)
                REFERENCES memory_candidates(candidate_id, candidate_version)
        );
        CREATE TRIGGER IF NOT EXISTS trg_candidate_merge_decisions_no_update
        BEFORE UPDATE ON candidate_merge_decisions
        BEGIN SELECT RAISE(ABORT, 'candidate_merge_decisions is append-only'); END;
        CREATE TRIGGER IF NOT EXISTS trg_candidate_merge_decisions_no_delete
        BEFORE DELETE ON candidate_merge_decisions
        BEGIN SELECT RAISE(ABORT, 'candidate_merge_decisions is append-only'); END;
        """
    )
    conn.commit()


def _migrate_v90(conn: sqlite3.Connection) -> None:
    """v90: Add persisted rollout, privacy-safe parity, and ingress receipts."""
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS learning_rollout_policies (
          policy_id TEXT PRIMARY KEY,surface TEXT NOT NULL,
          cohort_type TEXT NOT NULL CHECK(cohort_type IN ('surface','operator','project','operator_project')),
          cohort_id TEXT NOT NULL,operator_id TEXT,project_id TEXT,
          mode TEXT NOT NULL CHECK(mode IN ('legacy','shadow','v2')),
          version INTEGER NOT NULL CHECK(version>=1),actor TEXT NOT NULL,reason TEXT NOT NULL,
          updated_at TEXT NOT NULL,UNIQUE(surface,cohort_type,cohort_id));
        CREATE TABLE IF NOT EXISTS learning_rollout_policy_events (
          event_id TEXT PRIMARY KEY,policy_id TEXT NOT NULL,surface TEXT NOT NULL,
          cohort_type TEXT NOT NULL,cohort_id TEXT NOT NULL,operator_id TEXT,project_id TEXT,
          old_mode TEXT,new_mode TEXT NOT NULL,policy_version INTEGER NOT NULL,
          actor TEXT NOT NULL,reason TEXT NOT NULL,created_at TEXT NOT NULL);
        CREATE TRIGGER IF NOT EXISTS trg_learning_rollout_events_no_update
          BEFORE UPDATE ON learning_rollout_policy_events BEGIN SELECT RAISE(ABORT,'rollout events are append-only'); END;
        CREATE TRIGGER IF NOT EXISTS trg_learning_rollout_events_no_delete
          BEFORE DELETE ON learning_rollout_policy_events BEGIN SELECT RAISE(ABORT,'rollout events are append-only'); END;
        CREATE TABLE IF NOT EXISTS learning_shadow_parity (
          telemetry_id TEXT PRIMARY KEY,surface TEXT NOT NULL,operator_id TEXT,project_id TEXT,
          cohort TEXT NOT NULL,policy_version INTEGER,trace_id TEXT,output_revision_id TEXT,
          legacy_hash TEXT NOT NULL,v2_hash TEXT NOT NULL,legacy_chars INTEGER NOT NULL,
          v2_chars INTEGER NOT NULL,changed_classification TEXT NOT NULL CHECK(changed_classification IN ('same','changed')),
          selected_facts_json TEXT NOT NULL DEFAULT '[]',error_category TEXT,created_at TEXT NOT NULL);
        CREATE INDEX IF NOT EXISTS idx_learning_shadow_parity_surface ON learning_shadow_parity(surface,created_at);
        CREATE TABLE IF NOT EXISTS learning_ingress_receipts (
          event_id TEXT PRIMARY KEY,source_type TEXT NOT NULL,source_id TEXT NOT NULL,
          operator_id TEXT NOT NULL,project_id TEXT,dual_write_requested INTEGER NOT NULL,
          v2_status TEXT NOT NULL,legacy_status TEXT NOT NULL,status TEXT NOT NULL,
          v2_receipt_json TEXT,legacy_receipt_json TEXT,v2_error TEXT,legacy_error TEXT,
          created_at TEXT NOT NULL,UNIQUE(source_type,source_id));
        """
    )
    conn.commit()


def _migrate_v91(conn: sqlite3.Connection) -> None:
    """v91: Add reversible, audited legacy learning retirement state."""
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS learning_legacy_retirement (
          singleton_id INTEGER PRIMARY KEY CHECK(singleton_id=1),
          mode TEXT NOT NULL CHECK(mode IN ('compatibility','read_only','retired')),
          version INTEGER NOT NULL CHECK(version>=1),actor TEXT NOT NULL,
          reason TEXT NOT NULL,updated_at TEXT NOT NULL);
        INSERT OR IGNORE INTO learning_legacy_retirement
          (singleton_id,mode,version,actor,reason,updated_at)
          VALUES (1,'compatibility',1,'migration','initial compatibility boundary',datetime('now'));
        CREATE TABLE IF NOT EXISTS learning_legacy_retirement_gates (
          gate_name TEXT PRIMARY KEY CHECK(gate_name IN
            ('backfill_complete','parity_acceptable','canary_stable','stable_cycle_recorded')),
          passed INTEGER NOT NULL CHECK(passed IN (0,1)),evidence_ref TEXT NOT NULL,
          actor TEXT NOT NULL,reason TEXT NOT NULL,recorded_at TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS learning_legacy_retirement_events (
          event_id TEXT PRIMARY KEY,old_mode TEXT NOT NULL,new_mode TEXT NOT NULL,
          state_version INTEGER NOT NULL,actor TEXT NOT NULL,reason TEXT NOT NULL,
          created_at TEXT NOT NULL);
        CREATE TRIGGER IF NOT EXISTS trg_legacy_retirement_events_no_update
          BEFORE UPDATE ON learning_legacy_retirement_events
          BEGIN SELECT RAISE(ABORT,'retirement events are append-only'); END;
        CREATE TRIGGER IF NOT EXISTS trg_legacy_retirement_events_no_delete
          BEFORE DELETE ON learning_legacy_retirement_events
          BEGIN SELECT RAISE(ABORT,'retirement events are append-only'); END;
        """
    )
    conn.commit()


def _migrate_v92(conn: sqlite3.Connection) -> None:
    """v92: Add fail-open shadow outbox and recoverable ingress attempt state."""
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS learning_shadow_parity_outbox (
          telemetry_id TEXT PRIMARY KEY,payload_json TEXT NOT NULL,
          status TEXT NOT NULL DEFAULT 'pending'
            CHECK(status IN ('pending','completed','dead_letter')),
          attempts INTEGER NOT NULL DEFAULT 0,last_error TEXT,
          created_at TEXT NOT NULL,completed_at TEXT);
        """
    )
    columns = {row[1] for row in conn.execute("PRAGMA table_info(learning_ingress_receipts)")}
    additions = {
        "v2_attempts": "INTEGER NOT NULL DEFAULT 0",
        "legacy_attempts": "INTEGER NOT NULL DEFAULT 0",
        "max_attempts": "INTEGER NOT NULL DEFAULT 3",
        "updated_at": "TEXT",
    }
    for name, ddl in additions.items():
        if name not in columns:
            conn.execute(f"ALTER TABLE learning_ingress_receipts ADD COLUMN {name} {ddl}")
    conn.execute(
        "UPDATE learning_ingress_receipts SET updated_at=COALESCE(updated_at,created_at,datetime('now'))"
    )
    conn.commit()



def _migrate_v81(conn: sqlite3.Connection) -> None:
    """v81: Reconcile every exact-ledger constraint, FK, and trigger."""
    if not _table_exists(conn, "learning_outcomes"):
        _migrate_v79(conn)
    if _exact_ledgers_complete(conn):
        return

    conn.commit()
    conn.execute("PRAGMA foreign_keys=OFF")
    conn.execute("PRAGMA legacy_alter_table=ON")
    try:
        for name in _EXACT_LEDGER_TRIGGERS:
            conn.execute(f"DROP TRIGGER IF EXISTS {name}")
        for table in ("learning_outcomes", "memory_exposures", "output_revisions"):
            conn.execute(f"DROP TABLE IF EXISTS {table}_v81_repair")
            conn.execute(f"ALTER TABLE {table} RENAME TO {table}_v81_repair")

        from src.db.schema import SCHEMA_DDL
        start = SCHEMA_DDL.index("CREATE TABLE IF NOT EXISTS output_revisions")
        end = SCHEMA_DDL.index("-- Agent Notes", start)
        conn.executescript(SCHEMA_DDL[start:end])
        conn.execute("""INSERT OR IGNORE INTO output_revisions
            (output_revision_id,operator_id,surface,artifact_type,artifact_id,revision,
             trace_id,content_hash,prior_output_revision_id,created_at)
            SELECT r.output_revision_id,r.operator_id,r.surface,r.artifact_type,
                   r.artifact_id,r.revision,r.trace_id,r.content_hash,
                   CASE WHEN p.output_revision_id IS NOT NULL
                        THEN r.prior_output_revision_id ELSE NULL END,r.created_at
            FROM output_revisions_v81_repair r
            LEFT JOIN output_revisions_v81_repair p
              ON p.output_revision_id=r.prior_output_revision_id
            WHERE r.output_revision_id IS NOT NULL AND r.operator_id IS NOT NULL
              AND r.surface IN ('chat','web','draft','advisory','tool')
              AND r.artifact_type IS NOT NULL AND r.artifact_id IS NOT NULL
              AND r.revision >= 1 AND r.content_hash IS NOT NULL
              AND r.created_at IS NOT NULL""")
        conn.execute("""INSERT OR IGNORE INTO memory_exposures
            (exposure_id,trace_id,output_revision_id,operator_id,surface,source_surface,
             task_type,project_id,fact_id,fact_version,rendered_hash,rank,token_count,created_at)
            SELECT e.exposure_id,e.trace_id,e.output_revision_id,e.operator_id,e.surface,
                   e.source_surface,e.task_type,e.project_id,e.fact_id,e.fact_version,
                   e.rendered_hash,e.rank,e.token_count,e.created_at
            FROM memory_exposures_v81_repair e
            JOIN output_revisions r ON r.output_revision_id=e.output_revision_id
            WHERE e.exposure_id IS NOT NULL AND e.trace_id IS NOT NULL
              AND e.operator_id IS NOT NULL AND e.surface=r.surface
              AND e.source_surface IN ('chat','web','draft','advisory','tool')
              AND e.fact_id IS NOT NULL AND e.fact_version >= 1
              AND e.rendered_hash IS NOT NULL AND e.rank >= 1 AND e.token_count >= 0
              AND e.created_at IS NOT NULL AND r.trace_id=e.trace_id
              AND r.operator_id=e.operator_id""")
        conn.execute("""INSERT OR IGNORE INTO learning_outcomes
            (outcome_id,idempotency_key,exposure_id,trace_id,output_revision_id,
             operator_id,surface,fact_id,fact_version,feedback_id,outcome_type,
             outcome_value,reason,actor_id,feedback_scope,created_at)
            SELECT o.outcome_id,o.idempotency_key,o.exposure_id,o.trace_id,
                   o.output_revision_id,o.operator_id,o.surface,o.fact_id,o.fact_version,
                   o.feedback_id,o.outcome_type,o.outcome_value,o.reason,o.actor_id,
                   CASE WHEN o.feedback_scope='all_rendered' THEN 'all_rendered' ELSE NULL END,
                   o.created_at
            FROM learning_outcomes_v81_repair o
            JOIN output_revisions r ON r.output_revision_id=o.output_revision_id
            JOIN memory_exposures e ON e.exposure_id=o.exposure_id
            WHERE o.exposure_id IS NOT NULL AND o.fact_id IS NOT NULL
              AND o.fact_version IS NOT NULL AND o.outcome_id IS NOT NULL
              AND o.idempotency_key IS NOT NULL AND o.actor_id IS NOT NULL
              AND o.outcome_type IN ('positive','negative','correction','send','client_reply')
              AND o.outcome_value IS NOT NULL AND o.created_at IS NOT NULL
              AND r.trace_id=o.trace_id AND r.operator_id=o.operator_id AND r.surface=o.surface
              AND e.output_revision_id=o.output_revision_id AND e.trace_id=o.trace_id
              AND e.operator_id=o.operator_id AND e.surface=o.surface
              AND e.fact_id=o.fact_id AND e.fact_version=o.fact_version""")
        for table in ("learning_outcomes", "memory_exposures", "output_revisions"):
            conn.execute(f"DROP TABLE {table}_v81_repair")
        conn.commit()
    finally:
        conn.execute("PRAGMA legacy_alter_table=OFF")
        conn.execute("PRAGMA foreign_keys=ON")
    if conn.execute("PRAGMA foreign_key_check").fetchall():
        raise RuntimeError("v81 foreign key integrity failure")
    if not _exact_ledgers_complete(conn):
        raise RuntimeError("v81 exact ledger reconciliation incomplete")


def _migrate_v80(conn: sqlite3.Connection) -> None:
    """v80: Rebuild exact ledgers with revision FKs and typed feedback scopes."""
    columns = {row[1] for row in conn.execute("PRAGMA table_info(memory_exposures)")}
    if "source_surface" in columns:
        return
    conn.commit()
    conn.execute("PRAGMA foreign_keys=OFF")
    try:
        for trigger in (
            "trg_output_revisions_no_update", "trg_output_revisions_no_delete",
            "trg_memory_exposures_no_update", "trg_memory_exposures_no_delete",
            "trg_learning_outcomes_no_update", "trg_learning_outcomes_no_delete",
        ):
            conn.execute(f"DROP TRIGGER IF EXISTS {trigger}")
        conn.execute("ALTER TABLE learning_outcomes RENAME TO learning_outcomes_v79")
        conn.execute("ALTER TABLE memory_exposures RENAME TO memory_exposures_v79")
        conn.execute("ALTER TABLE output_revisions RENAME TO output_revisions_v79")
        from src.db.schema import SCHEMA_DDL
        start = SCHEMA_DDL.index("CREATE TABLE IF NOT EXISTS output_revisions")
        end = SCHEMA_DDL.index("-- Agent Notes", start)
        conn.executescript(SCHEMA_DDL[start:end])
        conn.execute("""INSERT INTO output_revisions
            (output_revision_id,operator_id,surface,artifact_type,artifact_id,revision,trace_id,content_hash,created_at)
            SELECT output_revision_id,operator_id,surface,artifact_type,artifact_id,revision,trace_id,content_hash,created_at
            FROM output_revisions_v79""")
        conn.execute("""INSERT INTO memory_exposures
            (exposure_id,trace_id,output_revision_id,operator_id,surface,source_surface,task_type,project_id,fact_id,fact_version,rendered_hash,rank,token_count,created_at)
            SELECT exposure_id,trace_id,output_revision_id,operator_id,surface,surface,task_type,project_id,fact_id,fact_version,rendered_hash,rank,token_count,created_at
            FROM memory_exposures_v79 WHERE output_revision_id IN (SELECT output_revision_id FROM output_revisions)""")
        conn.execute("""INSERT INTO learning_outcomes
            (outcome_id,idempotency_key,exposure_id,trace_id,output_revision_id,operator_id,surface,fact_id,fact_version,feedback_id,outcome_type,outcome_value,reason,actor_id,created_at)
            SELECT outcome_id,idempotency_key,exposure_id,trace_id,output_revision_id,operator_id,surface,fact_id,fact_version,feedback_id,outcome_type,outcome_value,reason,actor_id,created_at
            FROM learning_outcomes_v79 WHERE exposure_id IN (SELECT exposure_id FROM memory_exposures)""")
        conn.execute("DROP TABLE learning_outcomes_v79")
        conn.execute("DROP TABLE memory_exposures_v79")
        conn.execute("DROP TABLE output_revisions_v79")
        conn.commit()
    finally:
        conn.execute("PRAGMA foreign_keys=ON")
    if conn.execute("PRAGMA foreign_key_check").fetchall():
        raise RuntimeError("v80 foreign key integrity failure")


def _migrate_v79(conn: sqlite3.Connection) -> None:
    """v79: Add immutable exact exposure and outcome attribution ledgers."""
    from src.db.schema import SCHEMA_DDL

    start = SCHEMA_DDL.index("CREATE TABLE IF NOT EXISTS output_revisions")
    end = SCHEMA_DDL.index("-- Agent Notes", start)
    conn.executescript(SCHEMA_DDL[start:end])


def _migrate_v78(conn: sqlite3.Connection) -> None:
    """v78: Recoverably add organization to the candidate scope contract."""
    old_check = "('contact','project','counterparty','operator','market','global')"
    new_check = "('contact','project','counterparty','operator','market','organization','global')"
    conn.commit()
    conn.execute("PRAGMA foreign_keys=OFF")
    conn.execute("PRAGMA legacy_alter_table=ON")
    try:
        row = conn.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name='memory_candidates'"
        ).fetchone()
        source_sql = row[0] if row else ""
        if source_sql and "'organization'" in source_sql:
            conn.execute("DROP TABLE IF EXISTS memory_candidates_v78")
            conn.commit()
            return
        if not source_sql or old_check not in source_sql:
            raise RuntimeError("v78 could not identify memory_candidates scope constraint")
        conn.execute("DROP TABLE IF EXISTS memory_candidates_v78")
        create_sql = source_sql.replace(
            "CREATE TABLE memory_candidates", "CREATE TABLE memory_candidates_v78", 1,
        ).replace(old_check, new_check, 1)
        columns = [item[1] for item in conn.execute("PRAGMA table_info(memory_candidates)")]
        names = ",".join(columns)
        before = conn.execute("SELECT COUNT(*) FROM memory_candidates").fetchone()[0]
        conn.execute(create_sql)
        _migration_checkpoint("v78_temp_created")
        conn.execute(f"INSERT INTO memory_candidates_v78({names}) SELECT {names} FROM memory_candidates")
        _migration_checkpoint("v78_data_copied")
        after = conn.execute("SELECT COUNT(*) FROM memory_candidates_v78").fetchone()[0]
        if before != after:
            raise RuntimeError("v78 candidate row-count mismatch")
        conn.execute("DROP TABLE memory_candidates")
        _migration_checkpoint("v78_source_dropped")
        conn.execute("ALTER TABLE memory_candidates_v78 RENAME TO memory_candidates")
        conn.executescript(_MEMORY_CANDIDATES_DDL)
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.execute("PRAGMA legacy_alter_table=OFF")
        conn.execute("PRAGMA foreign_keys=ON")
        if conn.execute("PRAGMA foreign_keys").fetchone()[0] != 1:
            raise RuntimeError("v78 failed to restore foreign key enforcement")
    violations = conn.execute("PRAGMA foreign_key_check").fetchall()
    if violations:
        raise RuntimeError(f"v78 foreign key violations: {violations}")
    if conn.execute("SELECT COUNT(*) FROM memory_candidates").fetchone()[0] != before:
        raise RuntimeError("v78 candidate integrity check failed")
    conn.commit()


def _migrate_v77(conn: sqlite3.Connection) -> None:
    """v77: Backfill historical fact events before heads and add head guards."""
    conn.execute("""INSERT OR IGNORE INTO memory_fact_state_events(event_id,fact_id,state,actor_id,reason,effective_at,created_at)
        SELECT 'mfs_migration_'||fact_id,fact_id,status,'migration','Backfilled legacy fact state',
        CASE WHEN status='active' THEN valid_from ELSE COALESCE(valid_to,valid_from) END,
        CASE WHEN status='active' THEN valid_from ELSE COALESCE(valid_to,valid_from) END
        FROM memory_facts f WHERE NOT EXISTS (SELECT 1 FROM memory_fact_state_events e WHERE e.fact_id=f.fact_id)""")
    conn.execute("DELETE FROM memory_fact_heads")
    conn.execute("""INSERT INTO memory_fact_heads(fact_key,fact_id,fact_version)
        SELECT f.fact_key,f.fact_id,f.fact_version FROM memory_facts f WHERE EXISTS (
          SELECT 1 FROM memory_fact_state_events e WHERE e.fact_id=f.fact_id AND e.state='active'
          AND e.event_id=(SELECT event_id FROM memory_fact_state_events WHERE fact_id=f.fact_id ORDER BY effective_at DESC,created_at DESC,rowid DESC LIMIT 1))""")
    conn.execute("CREATE TRIGGER IF NOT EXISTS trg_memory_fact_heads_no_update BEFORE UPDATE ON memory_fact_heads BEGIN SELECT RAISE(ABORT,'memory_fact_heads is derived'); END")
    bad=conn.execute("""SELECT 1 FROM memory_fact_heads h WHERE NOT EXISTS (SELECT 1 FROM memory_facts f WHERE f.fact_id=h.fact_id AND f.fact_key=h.fact_key AND f.fact_version=h.fact_version) OR NOT EXISTS (SELECT 1 FROM memory_fact_state_events e WHERE e.fact_id=h.fact_id AND e.state='active' AND e.event_id=(SELECT event_id FROM memory_fact_state_events WHERE fact_id=h.fact_id ORDER BY effective_at DESC,created_at DESC,rowid DESC LIMIT 1)) LIMIT 1""").fetchone()
    if bad: raise RuntimeError("memory fact head backfill integrity failure")
    conn.commit()


def _migration_checkpoint(_phase: str) -> None:
    """Monkeypatchable failure checkpoint for migration recovery tests."""


def _migrate_v76(conn: sqlite3.Connection) -> None:
    """Recover projection parent/child tables and restore enforced final FKs."""
    conn.commit(); conn.execute("PRAGMA foreign_keys=OFF")
    try:
        if _table_exists(conn, "memory_projection_outbox") and not _table_exists(conn, "memory_projection_outbox_v75"):
            conn.execute("ALTER TABLE memory_projection_outbox RENAME TO memory_projection_outbox_v75"); _migration_checkpoint("parent_renamed")
        if _table_exists(conn, "memory_projection_deliveries") and not _table_exists(conn, "memory_projection_deliveries_v75"):
            conn.execute("ALTER TABLE memory_projection_deliveries RENAME TO memory_projection_deliveries_v75"); _migration_checkpoint("child_renamed")
        conn.execute("CREATE TABLE IF NOT EXISTS memory_projection_outbox (event_id TEXT PRIMARY KEY,fact_id TEXT NOT NULL REFERENCES memory_facts(fact_id),event_type TEXT NOT NULL CHECK(event_type IN ('upsert','delete')),status TEXT NOT NULL CHECK(status IN ('pending','completed','partial_failed','dead_letter')) DEFAULT 'pending',attempts INTEGER NOT NULL DEFAULT 0,last_error TEXT,created_at TEXT NOT NULL DEFAULT(datetime('now')),completed_at TEXT)")
        conn.execute("CREATE TABLE IF NOT EXISTS memory_projection_deliveries (event_id TEXT NOT NULL REFERENCES memory_projection_outbox(event_id),target TEXT NOT NULL CHECK(target IN ('learned_rules','response_pattern','operator_memory')),status TEXT NOT NULL CHECK(status IN ('pending','completed','dead_letter')) DEFAULT 'pending',attempts INTEGER NOT NULL DEFAULT 0,last_error TEXT,completed_at TEXT,PRIMARY KEY(event_id,target))"); _migration_checkpoint("final_created")
        if _table_exists(conn, "memory_projection_outbox_v75"):
            conn.execute("INSERT OR IGNORE INTO memory_projection_outbox SELECT * FROM memory_projection_outbox_v75"); _migration_checkpoint("parent_copied")
        if _table_exists(conn, "memory_projection_deliveries_v75"):
            conn.execute("INSERT OR IGNORE INTO memory_projection_deliveries SELECT * FROM memory_projection_deliveries_v75"); _migration_checkpoint("child_copied")
        if _table_exists(conn, "memory_projection_outbox_v75"):
            old={r[0] for r in conn.execute("SELECT event_id FROM memory_projection_outbox_v75")}; new={r[0] for r in conn.execute("SELECT event_id FROM memory_projection_outbox")};
            if not old <= new: raise RuntimeError("outbox reconciliation lost rows")
        if _table_exists(conn, "memory_projection_deliveries_v75"):
            old={(r[0],r[1]) for r in conn.execute("SELECT event_id,target FROM memory_projection_deliveries_v75")}; new={(r[0],r[1]) for r in conn.execute("SELECT event_id,target FROM memory_projection_deliveries")};
            if not old <= new: raise RuntimeError("delivery reconciliation lost rows")
        conn.execute("DROP TABLE IF EXISTS memory_projection_deliveries_v75"); _migration_checkpoint("child_dropped")
        conn.execute("DROP TABLE IF EXISTS memory_projection_outbox_v75"); _migration_checkpoint("parent_dropped")
        conn.commit()
    except Exception:
        conn.rollback(); raise
    finally:
        if conn.in_transaction: conn.rollback()
        conn.execute("PRAGMA foreign_keys=ON")
        if conn.execute("PRAGMA foreign_keys").fetchone()[0] != 1:
            raise RuntimeError("failed to restore SQLite foreign key enforcement")
    violations=conn.execute("PRAGMA foreign_key_check").fetchall(); _migration_checkpoint("fk_checked")
    if violations: raise RuntimeError(f"v76 foreign key violations: {violations}")


def _migrate_v75(conn: sqlite3.Connection) -> None:
    """v75: Remove obsolete active index and widen projection aggregate states."""
    conn.execute("DROP INDEX IF EXISTS idx_memory_facts_one_active")
    row = conn.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name='memory_projection_outbox'").fetchone()
    if row and "partial_failed" not in row[0]:
        conn.execute("PRAGMA foreign_keys=OFF")
        conn.executescript("""ALTER TABLE memory_projection_outbox RENAME TO memory_projection_outbox_v74;
        CREATE TABLE memory_projection_outbox (event_id TEXT PRIMARY KEY,fact_id TEXT NOT NULL REFERENCES memory_facts(fact_id),event_type TEXT NOT NULL CHECK(event_type IN ('upsert','delete')),status TEXT NOT NULL CHECK(status IN ('pending','completed','partial_failed','dead_letter')) DEFAULT 'pending',attempts INTEGER NOT NULL DEFAULT 0,last_error TEXT,created_at TEXT NOT NULL DEFAULT(datetime('now')),completed_at TEXT);
        INSERT INTO memory_projection_outbox SELECT * FROM memory_projection_outbox_v74;
        DROP TABLE memory_projection_outbox_v74;""")
        conn.execute("PRAGMA foreign_keys=ON")
    conn.commit()


def _migrate_v74(conn: sqlite3.Connection) -> None:
    """v74: Create state tables, backfill events, then enable guarded heads."""
    conn.execute("CREATE TABLE IF NOT EXISTS memory_fact_heads (fact_key TEXT PRIMARY KEY,fact_id TEXT NOT NULL UNIQUE REFERENCES memory_facts(fact_id),fact_version INTEGER NOT NULL)")
    conn.execute("CREATE TABLE IF NOT EXISTS memory_fact_state_events (event_id TEXT PRIMARY KEY,fact_id TEXT NOT NULL REFERENCES memory_facts(fact_id),state TEXT NOT NULL CHECK(state IN ('active','superseded','revoked')),actor_id TEXT NOT NULL,reason TEXT NOT NULL,effective_at TEXT NOT NULL,created_at TEXT NOT NULL DEFAULT(datetime('now')))")
    conn.execute("""INSERT OR IGNORE INTO memory_fact_state_events(event_id,fact_id,state,actor_id,reason,effective_at,created_at)
        SELECT 'mfs_migration_'||fact_id,fact_id,status,'migration','Backfilled legacy fact state',CASE WHEN status='active' THEN valid_from ELSE COALESCE(valid_to,valid_from) END,CASE WHEN status='active' THEN valid_from ELSE COALESCE(valid_to,valid_from) END FROM memory_facts f WHERE NOT EXISTS (SELECT 1 FROM memory_fact_state_events e WHERE e.fact_id=f.fact_id)""")
    conn.execute("DELETE FROM memory_fact_heads")
    conn.execute("""INSERT INTO memory_fact_heads(fact_key,fact_id,fact_version) SELECT f.fact_key,f.fact_id,f.fact_version FROM memory_facts f WHERE EXISTS (SELECT 1 FROM memory_fact_state_events e WHERE e.fact_id=f.fact_id AND e.state='active' AND e.event_id=(SELECT event_id FROM memory_fact_state_events WHERE fact_id=f.fact_id ORDER BY effective_at DESC,created_at DESC,rowid DESC LIMIT 1))""")
    conn.execute("CREATE TRIGGER IF NOT EXISTS trg_memory_fact_heads_binding BEFORE INSERT ON memory_fact_heads WHEN NOT EXISTS (SELECT 1 FROM memory_facts f WHERE f.fact_id=NEW.fact_id AND f.fact_key=NEW.fact_key AND f.fact_version=NEW.fact_version) OR NOT EXISTS (SELECT 1 FROM memory_fact_state_events e WHERE e.fact_id=NEW.fact_id AND e.state='active' AND e.event_id=(SELECT event_id FROM memory_fact_state_events WHERE fact_id=NEW.fact_id ORDER BY effective_at DESC,created_at DESC,rowid DESC LIMIT 1)) BEGIN SELECT RAISE(ABORT,'memory_fact_heads binding invalid'); END")
    conn.executescript("""
    CREATE TRIGGER IF NOT EXISTS trg_memory_fact_state_no_update BEFORE UPDATE ON memory_fact_state_events BEGIN SELECT RAISE(ABORT,'memory_fact_state_events is append-only'); END;
    CREATE TRIGGER IF NOT EXISTS trg_memory_fact_state_no_delete BEFORE DELETE ON memory_fact_state_events BEGIN SELECT RAISE(ABORT,'memory_fact_state_events is append-only'); END;
    CREATE TABLE IF NOT EXISTS memory_projection_deliveries (event_id TEXT NOT NULL REFERENCES memory_projection_outbox(event_id),target TEXT NOT NULL CHECK(target IN ('learned_rules','response_pattern','operator_memory')),status TEXT NOT NULL CHECK(status IN ('pending','completed','dead_letter')) DEFAULT 'pending',attempts INTEGER NOT NULL DEFAULT 0,last_error TEXT,completed_at TEXT,PRIMARY KEY(event_id,target));
    CREATE TABLE IF NOT EXISTS memory_projection_ownership (target TEXT NOT NULL,legacy_key TEXT NOT NULL,fact_id TEXT NOT NULL REFERENCES memory_facts(fact_id),fact_version INTEGER NOT NULL,snapshot_json TEXT,PRIMARY KEY(target,legacy_key));
    CREATE TRIGGER IF NOT EXISTS trg_memory_fact_heads_no_update BEFORE UPDATE ON memory_fact_heads BEGIN SELECT RAISE(ABORT,'memory_fact_heads is derived'); END;
    """)
    conn.execute("INSERT OR IGNORE INTO memory_projection_deliveries(event_id,target) SELECT event_id,target FROM memory_projection_outbox CROSS JOIN (SELECT 'learned_rules' target UNION ALL SELECT 'response_pattern' UNION ALL SELECT 'operator_memory')")
    conn.commit()


def _migrate_v73(conn: sqlite3.Connection) -> None:
    """v73: Add immutable decisions, versioned facts, audit, and projection outbox."""
    from src.db.schema import SCHEMA_DDL
    start = SCHEMA_DDL.index("CREATE TABLE IF NOT EXISTS memory_decisions")
    end = SCHEMA_DDL.index("-- Internal actions (v39)")
    conn.executescript(SCHEMA_DDL[start:end])
    conn.commit()


def _migrate_v72(conn: sqlite3.Connection) -> None:
    """v72: Add independent suite, config, replay, and runtime hashes."""
    columns={row[1] for row in conn.execute("PRAGMA table_info(candidate_evaluations)")}
    for name in ("suite_hash","config_hash","replay_hash","runtime_hash"):
        if name not in columns:
            conn.execute(f"ALTER TABLE candidate_evaluations ADD COLUMN {name} TEXT NOT NULL DEFAULT ''")
    conn.commit()


def _migrate_v64(conn: sqlite3.Connection) -> None:
    """v64: Persist replayable assessment metadata on proposed updates."""
    existing = {row[1] for row in conn.execute("PRAGMA table_info(proposed_updates)")}
    for col, typedef in [
        ("assessment_json", "TEXT"),
        ("policy_version", "TEXT"),
        ("source_type", "TEXT"),
        ("source_ref", "TEXT"),
    ]:
        if col not in existing:
            conn.execute(f"ALTER TABLE proposed_updates ADD COLUMN {col} {typedef}")
    conn.commit()


def _migrate_v59(conn: sqlite3.Connection) -> None:
    """v59: Add shared, auditable meeting preparation snapshots."""
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS meeting_prep_snapshots (
            prep_id TEXT PRIMARY KEY,
            operator_id TEXT NOT NULL DEFAULT 'default_operator',
            calendar_event_id TEXT NOT NULL REFERENCES calendar_events(calendar_event_id),
            event_raw_hash TEXT NOT NULL,
            project_id TEXT REFERENCES projects(project_id),
            payload_json TEXT NOT NULL,
            generated_at TEXT NOT NULL,
            UNIQUE(operator_id, calendar_event_id, event_raw_hash)
        );
        CREATE INDEX IF NOT EXISTS idx_meeting_prep_event
            ON meeting_prep_snapshots(operator_id, calendar_event_id, generated_at DESC);
        """
    )
    conn.commit()


def _migrate_v58(conn: sqlite3.Connection) -> None:
    """v58: Add read-only Microsoft Graph calendar cache."""
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS calendar_events (
            calendar_event_id TEXT PRIMARY KEY,
            operator_id TEXT NOT NULL DEFAULT 'default_operator',
            calendar_id TEXT NOT NULL,
            subject TEXT NOT NULL,
            start_at TEXT NOT NULL,
            end_at TEXT NOT NULL,
            original_timezone TEXT,
            location TEXT,
            organizer_email TEXT,
            attendees_json TEXT NOT NULL DEFAULT '[]',
            is_online_meeting INTEGER NOT NULL DEFAULT 0,
            join_url TEXT,
            show_as TEXT,
            sensitivity TEXT,
            body_preview TEXT,
            last_modified_at TEXT,
            source_etag TEXT,
            raw_hash TEXT NOT NULL,
            synced_at TEXT NOT NULL,
            cancelled INTEGER NOT NULL DEFAULT 0
        );
        CREATE INDEX IF NOT EXISTS idx_calendar_events_time
            ON calendar_events(operator_id, start_at, end_at);
        CREATE TABLE IF NOT EXISTS calendar_sync_state (
            operator_id TEXT NOT NULL,
            calendar_id TEXT NOT NULL,
            provider TEXT NOT NULL DEFAULT 'msgraph',
            delta_token TEXT,
            last_success_at TEXT,
            last_error_at TEXT,
            last_error TEXT,
            updated_at TEXT NOT NULL DEFAULT (datetime('now')),
            PRIMARY KEY(operator_id, calendar_id, provider)
        );
        """
    )
    conn.commit()


def _migrate_v57(conn: sqlite3.Connection) -> None:
    """v57: Add actionable inbox intelligence insights."""
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS inbox_insights (
            insight_id TEXT PRIMARY KEY,
            email_id TEXT NOT NULL REFERENCES processed_emails(email_id),
            operator_id TEXT NOT NULL DEFAULT 'default_operator',
            priority TEXT NOT NULL CHECK (priority IN ('low','normal','high','urgent')),
            priority_reasons_json TEXT NOT NULL DEFAULT '[]',
            action_required INTEGER NOT NULL DEFAULT 0,
            request_summary TEXT,
            suggested_action TEXT,
            due_at TEXT,
            proposed_owner TEXT,
            related_entities_json TEXT NOT NULL DEFAULT '[]',
            evidence_spans_json TEXT NOT NULL DEFAULT '[]',
            confidence REAL NOT NULL DEFAULT 0.0 CHECK (confidence >= 0 AND confidence <= 1),
            review_status TEXT NOT NULL DEFAULT 'not_required' CHECK (
                review_status IN ('proposed','confirmed','rejected','not_required')
            ),
            category TEXT NOT NULL DEFAULT 'fyi',
            suppressed INTEGER NOT NULL DEFAULT 0,
            suppression_reason TEXT,
            dedup_key TEXT NOT NULL,
            created_at TEXT NOT NULL DEFAULT (datetime('now')),
            updated_at TEXT NOT NULL DEFAULT (datetime('now')),
            UNIQUE(operator_id, dedup_key)
        );
        CREATE INDEX IF NOT EXISTS idx_inbox_insights_priority
            ON inbox_insights(operator_id, priority, action_required, created_at DESC);
        CREATE INDEX IF NOT EXISTS idx_inbox_insights_email
            ON inbox_insights(email_id);
        """
    )
    conn.commit()


def _migrate_v56(conn: sqlite3.Connection) -> None:
    """v56: Expand operator memory to entity-scoped personal assistant contexts."""
    row = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'operator_memory'"
    ).fetchone()
    table_sql = (row[0] or "") if row else ""
    if "'calendar_event'" in table_sql:
        return
    conn.execute("PRAGMA foreign_keys = OFF")
    conn.execute("PRAGMA legacy_alter_table = ON")
    conn.executescript(
        """
        ALTER TABLE operator_memory RENAME TO operator_memory_v55;
        CREATE TABLE operator_memory (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            operator_id TEXT NOT NULL,
            scope TEXT NOT NULL CHECK (scope IN (
                'global', 'project', 'chat', 'counterparty', 'contact',
                'organisation', 'work_item', 'topic', 'calendar_event'
            )),
            scope_id TEXT NOT NULL DEFAULT '*',
            memory_type TEXT NOT NULL CHECK (
                memory_type IN ('semantic', 'procedural', 'forbidden', 'episodic_snippet')
            ),
            memory_key TEXT NOT NULL,
            memory_value TEXT NOT NULL,
            source TEXT NOT NULL DEFAULT 'operator'
                CHECK (source IN ('operator', 'memory_flush', 'dashboard', 'migration', 'eval')),
            active INTEGER NOT NULL DEFAULT 1,
            created_at TEXT NOT NULL DEFAULT (datetime('now')),
            updated_at TEXT NOT NULL DEFAULT (datetime('now')),
            UNIQUE (operator_id, scope, scope_id, memory_type, memory_key)
        );
        INSERT INTO operator_memory
        SELECT id, operator_id, scope, scope_id, memory_type, memory_key,
               memory_value, source, active, created_at, updated_at
        FROM operator_memory_v55;
        DROP TABLE operator_memory_v55;
        CREATE INDEX IF NOT EXISTS idx_operator_memory_lookup
            ON operator_memory (operator_id, scope, scope_id, memory_type, active);
        """
    )
    conn.execute("PRAGMA legacy_alter_table = OFF")
    conn.execute("PRAGMA foreign_keys = ON")
    conn.commit()


def _migrate_v55(conn: sqlite3.Connection) -> None:
    """v55: Add shared personal brief snapshots."""
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS personal_brief_snapshots (
            snapshot_id TEXT PRIMARY KEY,
            operator_id TEXT NOT NULL DEFAULT 'default_operator',
            generated_at TEXT NOT NULL,
            payload_json TEXT NOT NULL,
            feedback_json TEXT NOT NULL DEFAULT '[]',
            created_at TEXT NOT NULL DEFAULT (datetime('now'))
        );
        CREATE INDEX IF NOT EXISTS idx_personal_brief_snapshots_operator
            ON personal_brief_snapshots(operator_id, generated_at DESC);
        """
    )
    conn.commit()


def _migrate_v54(conn: sqlite3.Connection) -> None:
    """v54: Add generic assistant schedules and durable run history."""
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS assistant_schedules (
            schedule_id TEXT PRIMARY KEY,
            operator_id TEXT NOT NULL DEFAULT 'default_operator',
            title TEXT NOT NULL,
            trigger_type TEXT NOT NULL CHECK (trigger_type IN ('at','rrule','interval')),
            trigger_value TEXT NOT NULL,
            timezone TEXT NOT NULL DEFAULT 'Asia/Hong_Kong',
            command_type TEXT NOT NULL,
            command_payload_json TEXT NOT NULL DEFAULT '{}',
            channel TEXT NOT NULL DEFAULT 'telegram' CHECK (channel IN ('telegram','dashboard')),
            urgency TEXT NOT NULL DEFAULT 'normal' CHECK (urgency IN ('normal','urgent','emergency')),
            enabled INTEGER NOT NULL DEFAULT 1,
            dedup_key TEXT,
            next_run_at TEXT,
            last_run_at TEXT,
            created_at TEXT NOT NULL DEFAULT (datetime('now')),
            updated_at TEXT NOT NULL DEFAULT (datetime('now'))
        );
        CREATE INDEX IF NOT EXISTS idx_assistant_schedules_due
            ON assistant_schedules(enabled, next_run_at);
        CREATE UNIQUE INDEX IF NOT EXISTS idx_assistant_schedules_dedup
            ON assistant_schedules(operator_id, dedup_key)
            WHERE dedup_key IS NOT NULL AND enabled = 1;
        CREATE TABLE IF NOT EXISTS assistant_schedule_runs (
            run_id TEXT PRIMARY KEY,
            schedule_id TEXT NOT NULL REFERENCES assistant_schedules(schedule_id),
            scheduled_for TEXT NOT NULL,
            status TEXT NOT NULL CHECK (status IN ('claimed','sent','completed','failed','missed')),
            attempt_count INTEGER NOT NULL DEFAULT 1,
            result_json TEXT,
            error TEXT,
            started_at TEXT NOT NULL DEFAULT (datetime('now')),
            finished_at TEXT,
            UNIQUE(schedule_id, scheduled_for)
        );
        CREATE INDEX IF NOT EXISTS idx_assistant_schedule_runs_schedule
            ON assistant_schedule_runs(schedule_id, started_at DESC);
        """
    )
    conn.commit()


def _migrate_v53(conn: sqlite3.Connection) -> None:
    """v53: Extend work items and add personal work item details."""
    row = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'work_items'"
    ).fetchone()
    table_sql = (row[0] or "") if row else ""
    if table_sql and "'commitment'" not in table_sql:
        conn.execute("PRAGMA foreign_keys = OFF")
        conn.execute("PRAGMA legacy_alter_table = ON")
        conn.executescript(
            """
            ALTER TABLE work_items RENAME TO work_items_v52;
            CREATE TABLE work_items (
                work_item_id TEXT PRIMARY KEY,
                work_item_type TEXT NOT NULL CHECK (
                    work_item_type IN (
                        'project','support_ticket','outreach','standalone',
                        'task','commitment','waiting_for','reminder'
                    )
                ),
                title TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'open' CHECK (
                    status IN ('open','resolved','closed','cancelled')
                ),
                priority TEXT DEFAULT 'medium' CHECK (
                    priority IN ('critical','high','medium','low')
                ),
                entity_table TEXT CHECK (
                    entity_table IN ('projects','support_tickets','personal_work_items')
                    OR entity_table IS NULL
                ),
                entity_id TEXT,
                created_at TEXT NOT NULL DEFAULT (datetime('now')),
                updated_at TEXT NOT NULL DEFAULT (datetime('now'))
            );
            INSERT INTO work_items
            SELECT work_item_id, work_item_type, title, status, priority,
                   entity_table, entity_id, created_at, updated_at
            FROM work_items_v52;
            DROP TABLE work_items_v52;
            CREATE INDEX IF NOT EXISTS idx_work_items_status ON work_items(status);
            CREATE INDEX IF NOT EXISTS idx_work_items_type ON work_items(work_item_type);
            CREATE INDEX IF NOT EXISTS idx_work_items_entity ON work_items(entity_table, entity_id);
            CREATE TRIGGER IF NOT EXISTS trg_work_items_updated_at
            AFTER UPDATE ON work_items
            BEGIN
                UPDATE work_items SET updated_at = datetime('now')
                WHERE work_item_id = NEW.work_item_id;
            END;
            """
        )
        conn.execute("PRAGMA legacy_alter_table = OFF")
        conn.execute("PRAGMA foreign_keys = ON")
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS personal_work_items (
            work_item_id TEXT PRIMARY KEY REFERENCES work_items(work_item_id),
            operator_id TEXT NOT NULL DEFAULT 'default_operator',
            kind TEXT NOT NULL CHECK (kind IN ('task','commitment','waiting_for','reminder')),
            description TEXT,
            owner_type TEXT NOT NULL DEFAULT 'operator' CHECK (
                owner_type IN ('operator','contact','team','unknown')
            ),
            owner_ref TEXT,
            related_entity_type TEXT,
            related_entity_id TEXT,
            due_at TEXT,
            planned_start_at TEXT,
            duration_minutes INTEGER,
            remind_at TEXT,
            snoozed_until TEXT,
            recurrence_rule TEXT,
            source_type TEXT NOT NULL DEFAULT 'manual' CHECK (
                source_type IN ('manual','email','meeting','telegram','agent')
            ),
            source_ref TEXT,
            evidence_text TEXT,
            confidence REAL NOT NULL DEFAULT 1.0 CHECK (confidence >= 0 AND confidence <= 1),
            review_status TEXT NOT NULL DEFAULT 'not_required' CHECK (
                review_status IN ('proposed','confirmed','rejected','not_required')
            ),
            dedup_key TEXT,
            completed_at TEXT,
            resolution_note TEXT,
            created_at TEXT NOT NULL DEFAULT (datetime('now')),
            updated_at TEXT NOT NULL DEFAULT (datetime('now'))
        );
        CREATE INDEX IF NOT EXISTS idx_personal_work_items_due
            ON personal_work_items(operator_id, review_status, due_at);
        CREATE INDEX IF NOT EXISTS idx_personal_work_items_related
            ON personal_work_items(related_entity_type, related_entity_id);
        CREATE INDEX IF NOT EXISTS idx_personal_work_items_remind
            ON personal_work_items(remind_at, snoozed_until);
        CREATE UNIQUE INDEX IF NOT EXISTS idx_personal_work_items_dedup
            ON personal_work_items(operator_id, dedup_key)
            WHERE dedup_key IS NOT NULL AND review_status != 'rejected';
        """
    )
    conn.commit()


def _migrate_v52(conn: sqlite3.Connection) -> None:
    """v52: Add approval-bound action intents for external operations."""
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS action_intents (
            action_id TEXT PRIMARY KEY,
            operator_id TEXT NOT NULL DEFAULT 'default_operator',
            action_type TEXT NOT NULL,
            risk_class TEXT NOT NULL CHECK (risk_class IN ('low','medium','high','critical')),
            target_type TEXT NOT NULL,
            target_id TEXT NOT NULL,
            target_version TEXT,
            payload_json TEXT NOT NULL,
            payload_hash TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'pending' CHECK (
                status IN (
                    'pending','approved','rejected','executing','executed',
                    'failed','expired','invalidated'
                )
            ),
            created_by_surface TEXT NOT NULL,
            approved_by TEXT,
            created_at TEXT NOT NULL DEFAULT (datetime('now')),
            expires_at TEXT NOT NULL,
            approved_at TEXT,
            executed_at TEXT,
            result_ref TEXT,
            error TEXT,
            idempotency_key TEXT UNIQUE
        );
        CREATE INDEX IF NOT EXISTS idx_action_intents_status
            ON action_intents(operator_id, status, created_at DESC);
        CREATE INDEX IF NOT EXISTS idx_action_intents_target
            ON action_intents(target_type, target_id, created_at DESC);
        """
    )
    conn.commit()


def _migrate_v45(conn: sqlite3.Connection) -> None:
    """v45: Dashboard-togglable agent harness flags on operator_state."""
    existing = {row[1] for row in conn.execute("PRAGMA table_info(operator_state)")}
    if "harness_require_wiki_progress" not in existing:
        conn.execute(
            "ALTER TABLE operator_state ADD COLUMN harness_require_wiki_progress INTEGER"
        )
    if "harness_auto_draft_rule58" not in existing:
        conn.execute(
            "ALTER TABLE operator_state ADD COLUMN harness_auto_draft_rule58 INTEGER"
        )
    conn.commit()


def _migrate_v44(conn: sqlite3.Connection) -> None:
    """v44: Remove vendor/counterparty emails from projects.client_email."""
    from config.defaults import VENDOR_EMAIL_DOMAINS
    from src.triage.client_emails import strip_vendor_emails_from_raw

    rows = conn.execute(
        "SELECT project_id, client_email FROM projects WHERE client_email IS NOT NULL"
    ).fetchall()
    for row in rows:
        cleaned = strip_vendor_emails_from_raw(row["client_email"], conn)
        if cleaned != row["client_email"]:
            conn.execute(
                "UPDATE projects SET client_email = ? WHERE project_id = ?",
                (cleaned, row["project_id"]),
            )
            logger.info(
                "Stripped vendor emails from %s client_email",
                row["project_id"],
            )

    for domain in VENDOR_EMAIL_DOMAINS:
        conn.execute(
            """UPDATE organisations
               SET org_type = 'vendor'
               WHERE LOWER(domains) LIKE ?
                 AND org_type != 'vendor'""",
            (f"%{domain}%",),
        )
    conn.commit()


def _migrate_v93(conn: sqlite3.Connection) -> None:
    """Add run-oriented tracing and encrypted event payload storage."""
    conn.executescript("\n-- Run-oriented trace explorer\nCREATE TABLE IF NOT EXISTS trace_runs (\n    run_id TEXT PRIMARY KEY, surface TEXT NOT NULL, task TEXT NOT NULL, project_id TEXT, session_id TEXT,\n    chat_id INTEGER, operator_id TEXT, status TEXT NOT NULL DEFAULT 'running', anomaly_flags_json TEXT,\n    exit_reason TEXT, started_at TEXT NOT NULL, ended_at TEXT, duration_ms INTEGER, metadata_json TEXT, implicit INTEGER DEFAULT 0\n);\nCREATE INDEX IF NOT EXISTS idx_trace_runs_started ON trace_runs(started_at DESC);\nCREATE INDEX IF NOT EXISTS idx_trace_runs_filter ON trace_runs(surface, task, status, project_id);\nCREATE TABLE IF NOT EXISTS trace_events (\n    event_id TEXT PRIMARY KEY, run_id TEXT NOT NULL REFERENCES trace_runs(run_id) ON DELETE CASCADE, sequence INTEGER NOT NULL,\n    event_type TEXT NOT NULL, round_index INTEGER, llm_trace_id TEXT REFERENCES llm_traces(trace_id), tool_name TEXT, status TEXT NOT NULL,\n    duration_ms INTEGER, metadata_json TEXT, payload_ciphertext TEXT, payload_nonce TEXT, payload_key_version TEXT, payload_hash TEXT,\n    created_at TEXT NOT NULL, UNIQUE(run_id, sequence)\n);\nCREATE INDEX IF NOT EXISTS idx_trace_events_run ON trace_events(run_id, sequence);\nCREATE TABLE IF NOT EXISTS trace_diagnostics (\n    diagnostic_id TEXT PRIMARY KEY, run_id TEXT NOT NULL REFERENCES trace_runs(run_id) ON DELETE CASCADE, trigger TEXT NOT NULL,\n    status TEXT NOT NULL, severity TEXT, root_cause TEXT, summary TEXT, evidence_json TEXT, recommendations_json TEXT,\n    diagnostic_model TEXT, diagnostic_trace_id TEXT REFERENCES llm_traces(trace_id), created_at TEXT NOT NULL, completed_at TEXT,\n    UNIQUE(run_id, trigger)\n);\nCREATE INDEX IF NOT EXISTS idx_trace_diagnostics_run ON trace_diagnostics(run_id, created_at DESC);\n")
    columns = {row[1] for row in conn.execute("PRAGMA table_info(llm_traces)").fetchall()}
    if "run_id" not in columns:
        conn.execute("ALTER TABLE llm_traces ADD COLUMN run_id TEXT REFERENCES trace_runs(run_id)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_llm_traces_run_id ON llm_traces(run_id)")


def _migrate_v94(conn: sqlite3.Connection) -> None:
    """Create the canonical, versioned FIX session configuration registry."""
    from src.fix_sessions.schema import ensure_fix_session_schema

    ensure_fix_session_schema(conn)


def _migrate_v95(conn: sqlite3.Connection) -> None:
    """Create Dashboard auth data and enforce v95 user data boundaries."""
    from src.auth.principals import SHARED_MAILBOX_PRINCIPAL
    from src.auth.repository import bootstrap_primary_admin
    from src.auth.schema import ensure_auth_schema

    ensure_auth_schema(conn)
    primary_admin = bootstrap_primary_admin(conn)
    legacy_operator = str(primary_admin["user_id"])
    tables = {
        row[0] for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()
    }

    for table in ("project_chat_messages", "support_chat_messages"):
        columns = {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}
        if table in tables and "owner_user_id" not in columns:
            conn.execute(
                f"ALTER TABLE {table} ADD COLUMN owner_user_id TEXT "
                "NOT NULL DEFAULT 'default_operator'"
            )
        if table in tables:
            conn.execute(
                f"UPDATE {table} SET owner_user_id=? "
                "WHERE owner_user_id IS NULL OR owner_user_id='' OR owner_user_id='default_operator'",
                (legacy_operator,),
            )

    if "project_chat_messages" in tables:
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_project_chat_owner "
            "ON project_chat_messages(project_id, owner_user_id, created_at)"
        )
    if "support_chat_messages" in tables:
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_support_chat_owner "
            "ON support_chat_messages(ticket_id, owner_user_id, created_at)"
        )

    if "inbox_insights" in tables:
        conn.execute(
            """DELETE FROM inbox_insights
               WHERE operator_id=? AND EXISTS (
                   SELECT 1 FROM inbox_insights shared
                   WHERE shared.operator_id=?
                     AND shared.dedup_key=inbox_insights.dedup_key
               )""",
            (legacy_operator, SHARED_MAILBOX_PRINCIPAL),
        )
        conn.execute(
            "UPDATE inbox_insights SET operator_id=? WHERE operator_id=?",
            (SHARED_MAILBOX_PRINCIPAL, legacy_operator),
        )
    if "calendar_sync_state" in tables:
        conn.execute(
            """DELETE FROM calendar_sync_state
               WHERE operator_id=? AND EXISTS (
                   SELECT 1 FROM calendar_sync_state shared
                   WHERE shared.operator_id=?
                     AND shared.calendar_id=calendar_sync_state.calendar_id
                     AND shared.provider=calendar_sync_state.provider
               )""",
            (legacy_operator, SHARED_MAILBOX_PRINCIPAL),
        )
        conn.execute(
            "UPDATE calendar_sync_state SET operator_id=? WHERE operator_id=?",
            (SHARED_MAILBOX_PRINCIPAL, legacy_operator),
        )
    if "calendar_events" in tables:
        conn.execute(
            "UPDATE calendar_events SET operator_id=? WHERE operator_id=?",
            (SHARED_MAILBOX_PRINCIPAL, legacy_operator),
        )

def _migrate_v96(conn: sqlite3.Connection) -> None:
    """Add local registration, verification, reset tokens, and local sessions."""
    from src.auth.schema import ensure_auth_schema

    # Auth-specific foreign keys are checked by ensure_auth_schema(). A global
    # foreign_key_check is intentionally deferred until all legacy schema
    # reconciliation has completed.
    ensure_auth_schema(conn)



def _migrate_v102(conn: sqlite3.Connection) -> None:
    """Add stable checklist keys and evidence-backed resolution metadata."""
    columns = {row[1] for row in conn.execute("PRAGMA table_info(closing_checklist)")}
    for column, typedef in [
        ("item_key", "TEXT"),
        ("evidence_ref", "TEXT"),
        ("resolution_note", "TEXT"),
        ("resolved_by", "TEXT"),
        ("resolved_at", "TEXT"),
        ("sign_off_note", "TEXT"),
        ("acknowledgements_json", "TEXT"),
    ]:
        if column not in columns:
            conn.execute(f"ALTER TABLE closing_checklist ADD COLUMN {column} {typedef}")

    key_by_label = {
        "All conformance tests passed": "conformance_complete",
        "Production session parameters confirmed": "production_session_ready",
        "Go-live date confirmed with counterparty": "go_live_ownership_confirmed",
        "Internal sign-off obtained": "production_authority_signoff",
        "Production monitoring configured": "production_monitoring_ready",
        "Client notified of go-live": "client_b2b_notified",
        "Network connectivity and session verified": "network_connectivity_verified",
        "Log review and documentation handover complete": "documentation_handover_complete",
        "All applicable conformance/PCT tests passed": "conformance_complete",
        "Production session parameters and readiness confirmed": "production_session_ready",
        "Named authority approved production enablement or handover": "production_authority_signoff",
        "Go-live date or post-handover owner confirmed": "go_live_ownership_confirmed",
        "Production monitoring and support ownership configured": "production_monitoring_ready",
        "Client or B2B owner notified of production handover": "client_b2b_notified",
    }
    for label, item_key in key_by_label.items():
        conn.execute(
            """UPDATE closing_checklist SET item_key = ?
               WHERE item_key IS NULL AND label = ?""",
            (item_key, label),
        )
    conn.execute(
        """UPDATE closing_checklist
           SET resolution_note = COALESCE(resolution_note, dismissed_reason),
               resolved_by = COALESCE(resolved_by, initiated_by),
               resolved_at = COALESCE(
                   resolved_at,
                   confirmed_at,
                   CASE WHEN status = 'dismissed' THEN initiated_at ELSE NULL END
               )
           WHERE status IN ('confirmed', 'dismissed')"""
    )
    # Preserve unexpected legacy duplicates without letting them collide with
    # the stable canonical key. Duplicate rows remain visible as legacy items,
    # so unresolved data blocks closing instead of being silently discarded.
    duplicate_rows = conn.execute(
        """SELECT item_id FROM (
               SELECT item_id,
                      ROW_NUMBER() OVER (
                          PARTITION BY project_id, item_key
                          ORDER BY CASE status
                              WHEN 'confirmed' THEN 0
                              WHEN 'dismissed' THEN 1
                              ELSE 2
                          END,
                          CASE WHEN evidence_ref IS NOT NULL THEN 0 ELSE 1 END,
                          COALESCE(resolved_at, confirmed_at, initiated_at) DESC,
                          item_id
                      ) AS duplicate_rank
               FROM closing_checklist
               WHERE item_key IS NOT NULL
           ) WHERE duplicate_rank > 1"""
    ).fetchall()
    for row in duplicate_rows:
        item_id = str(row[0])
        conn.execute(
            "UPDATE closing_checklist SET item_key = ? WHERE item_id = ?",
            (f"legacy_duplicate:{item_id}", item_id),
        )

    conn.execute(
        """CREATE UNIQUE INDEX IF NOT EXISTS idx_closing_checklist_project_key
           ON closing_checklist(project_id, item_key)
           WHERE item_key IS NOT NULL"""
    )


def _migrate_v103(conn: sqlite3.Connection) -> None:
    """Add vendor-neutral engineering trace identities and LLM timing metadata."""
    columns_by_table = {
        "trace_runs": [
            ("schema_version", "INTEGER NOT NULL DEFAULT 1"),
            ("trace_id", "TEXT"),
            ("turn_id", "TEXT"),
            ("thread_id", "TEXT"),
            ("component", "TEXT"),
            ("component_version", "TEXT"),
            ("persistence_status", "TEXT"),
        ],
        "trace_events": [
            ("schema_version", "INTEGER NOT NULL DEFAULT 1"),
            ("trace_id", "TEXT"),
            ("turn_id", "TEXT"),
            ("thread_id", "TEXT"),
            ("parent_event_id", "TEXT"),
            ("parent_span_id", "TEXT"),
            ("llm_call_id", "TEXT"),
            ("tool_call_id", "TEXT"),
            ("component", "TEXT"),
            ("component_version", "TEXT"),
        ],
        "llm_traces": [
            ("llm_call_id", "TEXT"),
            ("turn_id", "TEXT"),
            ("round_index", "INTEGER"),
            ("ttft_ms", "REAL"),
            ("retry_count", "INTEGER DEFAULT 0"),
            ("failover_from", "TEXT"),
            ("cache_hit", "INTEGER"),
            ("cache_read_tokens", "INTEGER DEFAULT 0"),
            ("cache_write_tokens", "INTEGER DEFAULT 0"),
            ("request_event_id", "TEXT"),
            ("response_event_id", "TEXT"),
        ],
    }
    for table, definitions in columns_by_table.items():
        existing = {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}
        for column, typedef in definitions:
            if column not in existing:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {typedef}")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_trace_runs_trace_id ON trace_runs(trace_id)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_trace_events_trace_id ON trace_events(trace_id, sequence)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_trace_events_llm_call ON trace_events(llm_call_id)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_trace_events_tool_call ON trace_events(tool_call_id)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_llm_traces_llm_call ON llm_traces(llm_call_id)")


def _migrate_v104(conn: sqlite3.Connection) -> None:
    """Add durable feedback linkage without retention-sensitive foreign keys."""
    existing = {row[1] for row in conn.execute("PRAGMA table_info(ai_feedback)")}
    for column in ("surface", "trace_id", "run_id", "turn_id", "output_revision_id"):
        if column not in existing:
            conn.execute(f"ALTER TABLE ai_feedback ADD COLUMN {column} TEXT")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_ai_feedback_trace_id ON ai_feedback(trace_id)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_ai_feedback_run_id ON ai_feedback(run_id)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_ai_feedback_turn_id ON ai_feedback(turn_id)")
    conn.commit()


def _migrate_v105(conn: sqlite3.Connection) -> None:
    """Add append-only, privacy-safe Email Project Truth B shadow telemetry."""
    conn.executescript(
        """CREATE TABLE IF NOT EXISTS email_project_truth_shadow (
            observation_id TEXT PRIMARY KEY,
            policy_version TEXT NOT NULL,
            project_id TEXT NOT NULL,
            source_ref_hash TEXT NOT NULL,
            parent_trace_id TEXT,
            parent_run_id TEXT,
            shadow_trace_id TEXT,
            shadow_run_id TEXT,
            baseline_status TEXT NOT NULL,
            candidate_status TEXT NOT NULL,
            baseline_field_hashes_json TEXT NOT NULL DEFAULT '{}',
            candidate_field_hashes_json TEXT NOT NULL DEFAULT '{}',
            comparison_classification TEXT NOT NULL,
            citation_coverage REAL NOT NULL DEFAULT 0
                CHECK (citation_coverage >= 0 AND citation_coverage <= 1),
            unsafe_update INTEGER NOT NULL DEFAULT 0 CHECK (unsafe_update IN (0,1)),
            review_required INTEGER NOT NULL DEFAULT 0 CHECK (review_required IN (0,1)),
            review_reason_count INTEGER NOT NULL DEFAULT 0 CHECK (review_reason_count >= 0),
            accepted_citations INTEGER NOT NULL DEFAULT 0 CHECK (accepted_citations >= 0),
            total_citations INTEGER NOT NULL DEFAULT 0 CHECK (total_citations >= 0),
            rejected_citations INTEGER NOT NULL DEFAULT 0 CHECK (rejected_citations >= 0),
            model TEXT,
            latency_ms INTEGER NOT NULL DEFAULT 0 CHECK (latency_ms >= 0),
            queue_lag_ms REAL NOT NULL DEFAULT 0 CHECK (queue_lag_ms >= 0),
            error_category TEXT,
            created_at TEXT NOT NULL DEFAULT (datetime('now'))
        );
        CREATE INDEX IF NOT EXISTS idx_email_project_truth_shadow_created
            ON email_project_truth_shadow(created_at DESC);
        CREATE INDEX IF NOT EXISTS idx_email_project_truth_shadow_project
            ON email_project_truth_shadow(project_id, created_at DESC);
        CREATE INDEX IF NOT EXISTS idx_email_project_truth_shadow_parent_run
            ON email_project_truth_shadow(parent_run_id);
        CREATE INDEX IF NOT EXISTS idx_email_project_truth_shadow_classification
            ON email_project_truth_shadow(comparison_classification, created_at DESC);
        CREATE TRIGGER IF NOT EXISTS trg_email_project_truth_shadow_no_update
        BEFORE UPDATE ON email_project_truth_shadow
        BEGIN
            SELECT RAISE(ABORT, 'email_project_truth_shadow is append-only');
        END;
        CREATE TRIGGER IF NOT EXISTS trg_email_project_truth_shadow_no_delete
        BEFORE DELETE ON email_project_truth_shadow
        BEGIN
            SELECT RAISE(ABORT, 'email_project_truth_shadow is append-only');
        END;
        """
    )
    conn.commit()


def _migrate_v106(conn: sqlite3.Connection) -> None:
    """Add durable checkpoints and proposal linkage for email reconciliation."""
    columns_by_table = {
        "pending_operations": [
            ("resource_key", "TEXT"),
            ("request_key", "TEXT"),
            ("phase", "TEXT NOT NULL DEFAULT 'queued'"),
            ("checkpoint_json", "TEXT NOT NULL DEFAULT '{}'"),
            ("result_json", "TEXT NOT NULL DEFAULT '{}'"),
            ("attempt_count", "INTEGER NOT NULL DEFAULT 0"),
            ("last_error", "TEXT"),
            ("retry_at", "TEXT"),
            ("lease_owner", "TEXT"),
            ("lease_expires_at", "TEXT"),
            ("updated_at", "TEXT"),
            ("trigger_kind", "TEXT NOT NULL DEFAULT 'operator'"),
            ("requested_commit", "INTEGER NOT NULL DEFAULT 0"),
            ("create_if_missing", "INTEGER NOT NULL DEFAULT 0"),
            ("trusted_surface", "TEXT"),
            ("proposed_update_id", "TEXT"),
        ],
        "proposed_updates": [
            ("target_project_id", "TEXT"),
            ("operation_id", "TEXT"),
        ],
    }
    for table, definitions in columns_by_table.items():
        existing = {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}
        for column, typedef in definitions:
            if column not in existing:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {typedef}")
    conn.execute(
        "UPDATE pending_operations SET phase='queued' "
        "WHERE phase IS NULL OR trim(phase)=''"
    )
    conn.execute(
        "UPDATE pending_operations SET updated_at=created_at "
        "WHERE updated_at IS NULL OR trim(updated_at)=''"
    )
    conn.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_pending_operations_request_key "
        "ON pending_operations(request_key) WHERE request_key IS NOT NULL"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_pending_operations_resource_phase "
        "ON pending_operations(resource_key, operation_type, phase, updated_at DESC)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_proposed_updates_operation "
        "ON proposed_updates(operation_id, created_at DESC)"
    )
    conn.commit()


def _migrate_v107(conn: sqlite3.Connection) -> None:
    """Allow obsolete reconciliation checkpoints to enter a terminal phase."""
    row = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='pending_operations'"
    ).fetchone()
    if row is None:
        from src.db.schema import apply_schema

        apply_schema(conn)
        return

    table_sql = str(row[0] or "")
    normalized = "".join(table_sql.lower().split())
    expected = (
        "phasein('queued','evaluating','needs_review','committed',"
        "'no_change','superseded','failed')"
    )
    if expected in normalized:
        return

    columns = (
        "operation_id", "operation_type", "operator_id", "chat_id",
        "project_id", "resource_key", "request_key", "payload_json",
        "base_version", "status", "phase", "checkpoint_json", "result_json",
        "attempt_count", "last_error", "retry_at", "lease_owner",
        "lease_expires_at", "updated_at", "trigger_kind", "requested_commit",
        "create_if_missing", "trusted_surface", "proposed_update_id",
        "created_at", "expires_at", "resolved_at", "resolution",
    )
    column_list = ", ".join(columns)

    conn.commit()
    conn.execute("PRAGMA foreign_keys = OFF")
    conn.execute("PRAGMA legacy_alter_table = ON")
    try:
        conn.executescript(
            """
            CREATE TABLE pending_operations_v107 (
                operation_id TEXT PRIMARY KEY,
                operation_type TEXT NOT NULL,
                operator_id TEXT NOT NULL DEFAULT 'default_operator',
                chat_id TEXT NOT NULL,
                project_id TEXT REFERENCES projects(project_id),
                resource_key TEXT,
                request_key TEXT,
                payload_json TEXT NOT NULL DEFAULT '{}',
                base_version TEXT,
                status TEXT NOT NULL DEFAULT 'pending' CHECK (
                    status IN ('pending','confirmed','cancelled','expired','superseded','failed')
                ),
                phase TEXT NOT NULL DEFAULT 'queued' CHECK (
                    phase IN (
                        'queued','evaluating','needs_review','committed',
                        'no_change','superseded','failed'
                    )
                ),
                checkpoint_json TEXT NOT NULL DEFAULT '{}',
                result_json TEXT NOT NULL DEFAULT '{}',
                attempt_count INTEGER NOT NULL DEFAULT 0,
                last_error TEXT,
                retry_at TEXT,
                lease_owner TEXT,
                lease_expires_at TEXT,
                updated_at TEXT,
                trigger_kind TEXT NOT NULL DEFAULT 'operator',
                requested_commit INTEGER NOT NULL DEFAULT 0 CHECK (requested_commit IN (0,1)),
                create_if_missing INTEGER NOT NULL DEFAULT 0 CHECK (create_if_missing IN (0,1)),
                trusted_surface TEXT,
                proposed_update_id TEXT,
                created_at TEXT NOT NULL,
                expires_at TEXT NOT NULL,
                resolved_at TEXT,
                resolution TEXT
            );
            """
        )
        conn.execute(
            f"INSERT INTO pending_operations_v107 ({column_list}) "
            f"SELECT {column_list} FROM pending_operations"
        )
        conn.executescript(
            """
            DROP TABLE pending_operations;
            ALTER TABLE pending_operations_v107 RENAME TO pending_operations;
            CREATE INDEX IF NOT EXISTS idx_pending_operations_scope
                ON pending_operations(operator_id, chat_id, status, created_at DESC);
            CREATE INDEX IF NOT EXISTS idx_pending_operations_project
                ON pending_operations(project_id, operation_type, status);
            CREATE INDEX IF NOT EXISTS idx_pending_operations_expiry
                ON pending_operations(expires_at, status);
            CREATE UNIQUE INDEX IF NOT EXISTS idx_pending_operations_request_key
                ON pending_operations(request_key) WHERE request_key IS NOT NULL;
            CREATE INDEX IF NOT EXISTS idx_pending_operations_resource_phase
                ON pending_operations(resource_key, operation_type, phase, updated_at DESC);
            """
        )
        conn.commit()
    finally:
        conn.execute("PRAGMA legacy_alter_table = OFF")
        conn.execute("PRAGMA foreign_keys = ON")

    violations = conn.execute("PRAGMA foreign_key_check(pending_operations)").fetchall()
    if violations:
        raise RuntimeError(f"pending_operations v107 FK repair failed: {violations}")


def _migrate_v108(conn: sqlite3.Connection) -> None:
    """Add team-identity columns used by Telegram binding and later four-eyes."""
    pu_cols = {row[1] for row in conn.execute("PRAGMA table_info(proposed_updates)")}
    if pu_cols and "decided_by" not in pu_cols:
        conn.execute("ALTER TABLE proposed_updates ADD COLUMN decided_by TEXT")
    draft_cols = {row[1] for row in conn.execute("PRAGMA table_info(drafts)")}
    if draft_cols and "claimed_by" not in draft_cols:
        conn.execute("ALTER TABLE drafts ADD COLUMN claimed_by TEXT")
    if draft_cols and "claimed_at" not in draft_cols:
        conn.execute("ALTER TABLE drafts ADD COLUMN claimed_at TEXT")
    conn.execute(
        """CREATE INDEX IF NOT EXISTS idx_external_identities_provider_object
           ON external_identities(provider, object_id)"""
    )
    conn.execute(
        """CREATE INDEX IF NOT EXISTS idx_drafts_claimed_by
           ON drafts(claimed_by, claimed_at)"""
    )
    conn.commit()


def _migrate_v109(conn: sqlite3.Connection) -> None:
    """Widen work_items CHECKs so personal tasks can be stored on older databases."""
    row = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'work_items'"
    ).fetchone()
    table_sql = (row[0] or "") if row else ""
    if not table_sql or "'task'" in table_sql:
        conn.commit()
        return
    conn.execute("PRAGMA foreign_keys = OFF")
    conn.execute("PRAGMA legacy_alter_table = ON")
    try:
        conn.executescript(
            """
            ALTER TABLE work_items RENAME TO work_items_v108;
            CREATE TABLE work_items (
                work_item_id TEXT PRIMARY KEY,
                work_item_type TEXT NOT NULL CHECK (
                    work_item_type IN (
                        'project','support_ticket','outreach','standalone',
                        'task','commitment','waiting_for','reminder'
                    )
                ),
                title TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'open' CHECK (
                    status IN ('open','resolved','closed','cancelled')
                ),
                priority TEXT DEFAULT 'medium' CHECK (
                    priority IN ('critical','high','medium','low')
                ),
                entity_table TEXT CHECK (
                    entity_table IN ('projects','support_tickets','personal_work_items')
                    OR entity_table IS NULL
                ),
                entity_id TEXT,
                created_at TEXT NOT NULL DEFAULT (datetime('now')),
                updated_at TEXT NOT NULL DEFAULT (datetime('now'))
            );
            INSERT INTO work_items
            SELECT work_item_id, work_item_type, title, status, priority,
                   entity_table, entity_id, created_at, updated_at
            FROM work_items_v108;
            DROP TABLE work_items_v108;
            CREATE INDEX IF NOT EXISTS idx_work_items_status ON work_items(status);
            CREATE INDEX IF NOT EXISTS idx_work_items_type ON work_items(work_item_type);
            CREATE INDEX IF NOT EXISTS idx_work_items_entity ON work_items(entity_table, entity_id);
            CREATE TRIGGER IF NOT EXISTS trg_work_items_updated_at
            AFTER UPDATE ON work_items
            BEGIN
                UPDATE work_items SET updated_at = datetime('now')
                WHERE work_item_id = NEW.work_item_id;
            END;
            """
        )
        conn.commit()
    finally:
        conn.execute("PRAGMA legacy_alter_table = OFF")
        conn.execute("PRAGMA foreign_keys = ON")


def _migrate_v110(conn: sqlite3.Connection) -> None:
    """Create the wiki compiler queue used by nightly nugget compilation."""
    conn.execute(
        """CREATE TABLE IF NOT EXISTS wiki_compiler_queue (
            queue_id TEXT PRIMARY KEY,
            entry_id TEXT NOT NULL UNIQUE REFERENCES email_knowledge_log(entry_id),
            domain TEXT,
            topic TEXT NOT NULL,
            state TEXT NOT NULL DEFAULT 'pending'
                CHECK (state IN ('pending','compiling','compiled','skipped','failed')),
            skip_reason TEXT,
            compiled_path TEXT,
            error TEXT,
            enqueued_at TEXT NOT NULL,
            compiled_at TEXT
        )"""
    )
    conn.execute(
        """CREATE INDEX IF NOT EXISTS idx_wiki_compiler_queue_state
           ON wiki_compiler_queue(state, topic, enqueued_at)"""
    )
    conn.commit()


def _migrate_v111(conn: sqlite3.Connection) -> None:
    """Add crash-safe checkpoints and typed outcomes to specialist sessions."""
    table = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='specialist_sessions'"
    ).fetchone()
    if table is None:
        conn.executescript(
            """
            CREATE TABLE specialist_sessions (
                session_id TEXT PRIMARY KEY,
                operator_id TEXT NOT NULL,
                thread_id TEXT NOT NULL,
                skill_id TEXT NOT NULL,
                specialist_type TEXT NOT NULL,
                project_id TEXT,
                parent_session_id TEXT,
                messages_json TEXT NOT NULL,
                tool_history_json TEXT,
                checkpoint_json TEXT NOT NULL DEFAULT '{}',
                checkpoint_seq INTEGER NOT NULL DEFAULT 0,
                status TEXT NOT NULL DEFAULT 'completed'
                    CHECK (status IN (
                        'in_progress', 'completed', 'partial', 'timed_out',
                        'exhausted', 'failed', 'unverified'
                    )),
                created_at TEXT NOT NULL DEFAULT (datetime('now')),
                updated_at TEXT NOT NULL DEFAULT (datetime('now')),
                expires_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_specialist_sessions_thread
                ON specialist_sessions (operator_id, thread_id, created_at DESC);
            CREATE INDEX IF NOT EXISTS idx_specialist_sessions_expires
                ON specialist_sessions (expires_at);
            """
        )
        conn.commit()
        return

    columns = {
        str(row[1]) for row in conn.execute("PRAGMA table_info(specialist_sessions)")
    }
    table_sql = str(table[0] or "").casefold()
    required = {"checkpoint_json", "checkpoint_seq", "updated_at"}
    typed_status_ready = all(
        marker in table_sql
        for marker in ("'in_progress'", "'partial'", "'timed_out'", "'unverified'")
    )
    if required.issubset(columns) and typed_status_ready:
        conn.execute(
            """CREATE INDEX IF NOT EXISTS idx_specialist_sessions_thread
               ON specialist_sessions (operator_id, thread_id, created_at DESC)"""
        )
        conn.execute(
            """CREATE INDEX IF NOT EXISTS idx_specialist_sessions_expires
               ON specialist_sessions (expires_at)"""
        )
        conn.commit()
        return

    checkpoint_expr = (
        "COALESCE(checkpoint_json, '{}')" if "checkpoint_json" in columns else "'{}'"
    )
    checkpoint_seq_expr = (
        "COALESCE(checkpoint_seq, 0)" if "checkpoint_seq" in columns else "0"
    )
    updated_at_expr = (
        "COALESCE(updated_at, created_at)" if "updated_at" in columns else "created_at"
    )
    conn.execute("DROP TABLE IF EXISTS specialist_sessions_v110")
    conn.execute("ALTER TABLE specialist_sessions RENAME TO specialist_sessions_v110")
    conn.execute(
        """CREATE TABLE specialist_sessions (
            session_id TEXT PRIMARY KEY,
            operator_id TEXT NOT NULL,
            thread_id TEXT NOT NULL,
            skill_id TEXT NOT NULL,
            specialist_type TEXT NOT NULL,
            project_id TEXT,
            parent_session_id TEXT,
            messages_json TEXT NOT NULL,
            tool_history_json TEXT,
            checkpoint_json TEXT NOT NULL DEFAULT '{}',
            checkpoint_seq INTEGER NOT NULL DEFAULT 0,
            status TEXT NOT NULL DEFAULT 'completed'
                CHECK (status IN (
                    'in_progress', 'completed', 'partial', 'timed_out',
                    'exhausted', 'failed', 'unverified'
                )),
            created_at TEXT NOT NULL DEFAULT (datetime('now')),
            updated_at TEXT NOT NULL DEFAULT (datetime('now')),
            expires_at TEXT NOT NULL
        )"""
    )
    conn.execute(
        f"""INSERT INTO specialist_sessions (
            session_id, operator_id, thread_id, skill_id, specialist_type,
            project_id, parent_session_id, messages_json, tool_history_json,
            checkpoint_json, checkpoint_seq, status, created_at, updated_at,
            expires_at
        )
        SELECT session_id, operator_id, thread_id, skill_id, specialist_type,
               project_id, parent_session_id, messages_json, tool_history_json,
               {checkpoint_expr}, {checkpoint_seq_expr}, status, created_at,
               {updated_at_expr}, expires_at
        FROM specialist_sessions_v110"""
    )
    conn.execute("DROP TABLE specialist_sessions_v110")
    conn.execute(
        """CREATE INDEX IF NOT EXISTS idx_specialist_sessions_thread
           ON specialist_sessions (operator_id, thread_id, created_at DESC)"""
    )
    conn.execute(
        """CREATE INDEX IF NOT EXISTS idx_specialist_sessions_expires
           ON specialist_sessions (expires_at)"""
    )
    conn.commit()


def _migrate_v112(conn: sqlite3.Connection) -> None:
    """Create the durable update-card outbox without rewriting existing updates."""
    from src.db.schema import EMAIL_UPDATE_NOTIFICATION_OUTBOX_DDL
    conn.executescript(EMAIL_UPDATE_NOTIFICATION_OUTBOX_DDL)
    conn.commit()


def _migrate_v113(conn: sqlite3.Connection) -> None:
    from src.db.followup_schema import ensure_followup_action_columns
    ensure_followup_action_columns(conn)
    conn.commit()


def _migrate_v114(conn: sqlite3.Connection) -> None:
    from src.db.followup_plan_schema import DDL
    conn.executescript(DDL)
    conn.commit()


def _migrate_v115(conn: sqlite3.Connection) -> None:
    from src.db.pct_completion_schema import DDL
    conn.executescript(DDL)
    conn.commit()


def _migrate_v116(conn: sqlite3.Connection) -> None:
    columns = {row[1] for row in conn.execute("PRAGMA table_info(processed_emails)")}
    if columns and "sent_at" not in columns:
        conn.execute("ALTER TABLE processed_emails ADD COLUMN sent_at TEXT")
    conn.commit()


def _migrate_v117(conn: sqlite3.Connection) -> None:
    from src.db.followup_review_schema import DDL
    conn.executescript(DDL)
    conn.commit()


def _migrate_v118(conn: sqlite3.Connection) -> None:
    from src.db.memory_state_sequence_schema import ensure_memory_event_sequence
    ensure_memory_event_sequence(conn)
    conn.commit()


def _migrate_v119(conn: sqlite3.Connection) -> None:
    """Add sourced actual dates without inferring dates from targets or closure."""
    from src.db.go_live_schema import DDL
    conn.executescript(DDL)
    conn.commit()


def _migrate_v120(conn: sqlite3.Connection) -> None:
    """Create the durable M8 Telegram delivery ledger without replaying old notices."""
    from src.db.schema import GO_LIVE_TELEGRAM_OUTBOX_DDL

    conn.executescript(GO_LIVE_TELEGRAM_OUTBOX_DDL)
    conn.commit()


def _migrate_v121(conn: sqlite3.Connection) -> None:
    """Persist support-email post-commit work without replaying old messages."""
    from src.db.schema import SUPPORT_EMAIL_EFFECTS_DDL

    conn.executescript(SUPPORT_EMAIL_EFFECTS_DDL)
    conn.commit()


def _migrate_v122(conn: sqlite3.Connection) -> None:
    """Make pending support drafts recoverable after process termination."""
    columns = {row[1] for row in conn.execute("PRAGMA table_info(support_email_effects)")}
    if not columns:
        return
    upgrading = "draft_lease_token" not in columns
    for name in ("draft_lease_token", "draft_lease_until", "draft_available_at", "draft_last_error"):
        if name not in columns:
            conn.execute(f"ALTER TABLE support_email_effects ADD COLUMN {name} TEXT")
    if upgrading:
        # A v121 daemon cannot survive a process restart. Completed drafts stay complete.
        conn.execute("""UPDATE support_email_effects SET draft_state='none'
            WHERE draft_state='scheduled' AND EXISTS (
                SELECT 1 FROM support_tickets t WHERE t.ticket_id=support_email_effects.ticket_id
                AND t.reply_draft_text IS NOT NULL)""")
        conn.execute("""UPDATE support_email_effects SET draft_state='pending'
            WHERE draft_state='scheduled'""")
    conn.commit()


def _migrate_v123(conn: sqlite3.Connection) -> None:
    """Create route work without pretending historical anchors were routed."""
    from src.db.inbound_work_schema import BASE_DDL

    conn.executescript(BASE_DDL)
    # Existing processed_emails anchors are not route receipts. Never replay
    # them automatically: the original side effects may have committed.
    conn.execute("""INSERT OR IGNORE INTO inbound_email_work
        (email_id, source_type, classification_json, route_state)
        SELECT email_id, 'legacy', '{}', 'needs_reconciliation'
        FROM processed_emails WHERE direction='inbound'""")
    conn.commit()


def _migrate_v124(conn: sqlite3.Connection) -> None:
    """Add reply-card intents; historical replies cannot safely be replayed."""
    from src.db.commitment_card_schema import DDL

    conn.executescript(DDL)
    conn.commit()


def _migrate_v125(conn: sqlite3.Connection) -> None:
    """Add only future overflow items; historical cards cannot be replayed."""
    from src.db.inbound_overflow_schema import DDL

    conn.executescript(DDL)
    conn.commit()


def _migrate_v126(conn: sqlite3.Connection) -> None:
    """Add an independent, non-replayable overflow resolution record."""
    columns = {row[1] for row in conn.execute("PRAGMA table_info(inbound_overflow_batches)")}
    if "review_version" not in columns:
        conn.execute("ALTER TABLE inbound_overflow_batches ADD COLUMN review_version INTEGER NOT NULL DEFAULT 0")
    conn.execute("""CREATE TABLE IF NOT EXISTS inbound_overflow_resolutions (
        batch_id TEXT PRIMARY KEY REFERENCES inbound_overflow_batches(batch_id),
        decision TEXT NOT NULL CHECK(decision='confirmed_sent'),
        message_ids_json TEXT NOT NULL,
        evidence_ref TEXT NOT NULL,
        actor TEXT NOT NULL,
        created_at TEXT NOT NULL DEFAULT (datetime('now'))
    )""")
    conn.commit()


def _migrate_v127(conn: sqlite3.Connection) -> None:
    """Create future inbound information intents; do not replay historical cards."""
    from src.db.inbound_presentation_schema import DDL

    conn.executescript(DDL)
    conn.commit()


def _migrate_v128(conn: sqlite3.Connection) -> None:
    """Create only future draft-card intents; do not replay historical drafts."""
    from src.db.draft_card_schema import DDL

    conn.executescript(DDL)
    conn.commit()


def _migrate_v129(conn: sqlite3.Connection) -> None:
    """Track attachment preparation before a draft card becomes deliverable."""
    columns = {row[1] for row in conn.execute("PRAGMA table_info(draft_card_outbox)")}
    if "attachment_state" not in columns:
        conn.execute(
            "ALTER TABLE draft_card_outbox ADD COLUMN "
            "attachment_state TEXT NOT NULL DEFAULT 'ready' "
            "CHECK(attachment_state IN ('ready','preparing','needs_review'))"
        )
    conn.commit()


def _migrate_v130(conn: sqlite3.Connection) -> None:
    """Create reviewable reminder send receipts without replaying historical claims."""
    from src.db.personal_reminder_schema import DDL

    conn.executescript(DDL)
    conn.commit()


def _migrate_v131(conn: sqlite3.Connection) -> None:
    """Version schedule intent independently of claims and completed deliveries."""
    for table in ("assistant_schedules", "assistant_schedule_runs"):
        columns = {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}
        if "intent_version" not in columns:
            conn.execute(
                f"ALTER TABLE {table} ADD COLUMN intent_version INTEGER NOT NULL DEFAULT 0"
            )
    conn.commit()


def _migrate_v132(conn: sqlite3.Connection) -> None:
    """Review linked-item failures without overwriting Telegram attestations."""
    columns = {
        row[1] for row in conn.execute("PRAGMA table_info(personal_reminder_deliveries)")
    }
    for column in (
        "linked_item_review_outcome",
        "linked_item_reviewer",
        "linked_item_evidence_ref",
        "linked_item_review_note",
        "linked_item_reviewed_at",
    ):
        if column not in columns:
            conn.execute(
                f"ALTER TABLE personal_reminder_deliveries ADD COLUMN {column} TEXT"
            )
    conn.commit()


def _migrate_v133(conn: sqlite3.Connection) -> None:
    """Record committed decisions for future journaled API attachment mutations."""
    from src.db.draft_attachment_schema import DDL

    conn.executescript(DDL)
    conn.commit()

def _migrate_v136(conn: sqlite3.Connection) -> None:
    """Add a separate, durable source retry rank to existing IMAP work."""
    columns = {row[1] for row in conn.execute("PRAGMA table_info(inbound_email_work)")}
    if "source_retry_count" not in columns:
        conn.execute("""ALTER TABLE inbound_email_work ADD COLUMN
            source_retry_count INTEGER NOT NULL DEFAULT 0
            CHECK (source_retry_count >= 0)""")
    conn.execute("""CREATE INDEX IF NOT EXISTS idx_inbound_work_imap_pending_retry
        ON inbound_email_work(source_type, account_key, mailbox, route_state,
                              source_retry_count, captured_at, email_id)""")
    conn.commit()


def _migrate_v137(conn: sqlite3.Connection) -> None:
    """Widen draft delivery CHECKs without changing rows or dependent foreign keys.

    SQLite cannot alter a CHECK in place. Rebuild each table from its own stored
    DDL so historical columns, defaults and constraints remain intact. Recreate
    all user-defined indexes and triggers (not just the known workspace ones).
    """
    replacements = []
    for table, column in (("drafts", "delivery_status"), ("draft_outbox", "status")):
        row = conn.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name=?", (table,)
        ).fetchone()
        if row is None or not row[0]:
            raise sqlite3.OperationalError(f"Missing {table} table for v137")
        ddl = row[0]
        pattern = rf"CHECK\s*\(\s*{column}\s+IN\s*\(([^()]*)\)\s*\)"
        checks = list(re.finditer(pattern, ddl, flags=re.IGNORECASE))
        if len(checks) != 1:
            raise sqlite3.OperationalError(f"Expected one {table}.{column} CHECK")
        if re.search(r"['\"]uncertain['\"]", checks[0].group(1), re.IGNORECASE):
            continue
        check = checks[0]
        widened = ddl[:check.end(1)] + ", 'uncertain'" + ddl[check.end(1):]
        name_pattern = rf'(CREATE\s+TABLE\s+(?:IF\s+NOT\s+EXISTS\s+)?)"?{table}"?(?=\s*\()'
        widened, count = re.subn(
            name_pattern, rf'\g<1>"{table}_v137"', widened,
            count=1, flags=re.IGNORECASE,
        )
        if count != 1:
            raise sqlite3.OperationalError(f"Cannot reconstruct {table} DDL")
        objects = [r[0] for r in conn.execute(
            "SELECT sql FROM sqlite_master WHERE tbl_name=? "
            "AND type IN ('index','trigger') AND sql IS NOT NULL ORDER BY type, name",
            (table,),
        )]
        columns = [r[1] for r in conn.execute(f'PRAGMA table_info("{table}")')]
        replacements.append((table, widened, columns, objects))

    if not replacements:
        return

    # PRAGMA foreign_keys only changes outside a transaction; avoid silently
    # leaving the caller's connection with a different enforcement setting.
    conn.commit()
    fk_enabled = conn.execute("PRAGMA foreign_keys").fetchone()[0]
    conn.execute("PRAGMA foreign_keys=OFF")
    try:
        conn.execute("BEGIN IMMEDIATE")
        for table, ddl, columns, objects in replacements:
            conn.execute(ddl)
            quoted = ", ".join('"' + col.replace('"', '""') + '"' for col in columns)
            conn.execute(
                f'INSERT INTO "{table}_v137" ({quoted}) '
                f'SELECT {quoted} FROM "{table}"'
            )
            conn.execute(f'DROP TABLE "{table}"')
            conn.execute(f'ALTER TABLE "{table}_v137" RENAME TO "{table}"')
            for sql in objects:
                conn.execute(sql)
        violations = conn.execute("PRAGMA foreign_key_check").fetchall()
        if violations:
            raise sqlite3.IntegrityError(f"v137 foreign key violations: {violations!r}")
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.execute(f"PRAGMA foreign_keys={fk_enabled}")


def _migrate_v138(conn: sqlite3.Connection) -> None:
    """Widen the action intent CHECK while preserving the stored table shape.

    SQLite cannot alter a CHECK in place. Use the existing DDL rather than a
    fixed template to keep site-specific columns, constraints, and defaults.
    """
    row = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='action_intents'"
    ).fetchone()
    if row is None or not row[0]:
        raise sqlite3.OperationalError("Missing action_intents table for v138")
    ddl = row[0]
    checks = list(re.finditer(
        r"CHECK\s*\(\s*status\s+IN\s*\(([^()]*)\)\s*\)",
        ddl, flags=re.IGNORECASE,
    ))
    if len(checks) != 1:
        raise sqlite3.OperationalError("Expected one action_intents.status CHECK")
    check = checks[0]
    if re.search(r"['\"]uncertain['\"]", check.group(1), re.IGNORECASE):
        return
    widened = ddl[:check.end(1)] + ", 'uncertain'" + ddl[check.end(1):]
    widened, count = re.subn(
        r'(CREATE\s+TABLE\s+(?:IF\s+NOT\s+EXISTS\s+)?)"?action_intents"?(?=\s*\()',
        r'\g<1>"action_intents_v138"', widened, count=1, flags=re.IGNORECASE,
    )
    if count != 1:
        raise sqlite3.OperationalError("Cannot reconstruct action_intents DDL")
    objects = [entry[0] for entry in conn.execute(
        "SELECT sql FROM sqlite_master WHERE tbl_name='action_intents' "
        "AND type IN ('index','trigger') AND sql IS NOT NULL ORDER BY type, name"
    )]
    columns = [entry[1] for entry in conn.execute('PRAGMA table_xinfo("action_intents")')
               if entry[6] == 0]
    quoted = ", ".join('"' + column.replace('"', '""') + '"' for column in columns)

    # Disabling FKs outside the transaction avoids cascading child deletes.
    conn.commit()
    fk_enabled = conn.execute("PRAGMA foreign_keys").fetchone()[0]
    conn.execute("PRAGMA foreign_keys=OFF")
    try:
        conn.execute("BEGIN IMMEDIATE")
        conn.execute(widened)
        conn.execute(
            f'INSERT INTO "action_intents_v138" ({quoted}) '
            f'SELECT {quoted} FROM "action_intents"'
        )
        conn.execute('DROP TABLE "action_intents"')
        conn.execute('ALTER TABLE "action_intents_v138" RENAME TO "action_intents"')
        for sql in objects:
            conn.execute(sql)
        violations = conn.execute("PRAGMA foreign_key_check").fetchall()
        if violations:
            raise sqlite3.IntegrityError(f"v138 foreign key violations: {violations!r}")
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.execute(f"PRAGMA foreign_keys={fk_enabled}")


def _migrate_v141(conn: sqlite3.Connection) -> None:
    """Create contact identity card intents, including on stamped databases."""
    from src.db.contact_identity_card_schema import DDL

    conn.executescript(DDL)


def _migrate_v140(conn: sqlite3.Connection) -> None:
    from src.db.project_advisory_schema import DDL

    conn.executescript(DDL)


def _migrate_v139(conn: sqlite3.Connection) -> None:
    """Add the operation-bound staged-update card outbox without replaying old cards."""
    from src.db.staged_update_card_schema import DDL

    conn.executescript(DDL)


def _migrate_v143(conn: sqlite3.Connection) -> None:
    from src.db.project_advisory_schema import ensure_advisory_delivery_columns

    ensure_advisory_delivery_columns(conn)
    conn.commit()


def _migrate_v142(conn: sqlite3.Connection) -> None:
    """Reconcile durable route provenance without guessing historical direct work."""
    from src.db.schema import SUPPORT_EMAIL_EFFECTS_DDL

    conn.executescript(SUPPORT_EMAIL_EFFECTS_DDL)
    columns = {row[1] for row in conn.execute("PRAGMA table_info(support_email_effects)")}
    if "route_source_type" not in columns:
        conn.execute("""ALTER TABLE support_email_effects ADD COLUMN
            route_source_type TEXT NOT NULL DEFAULT 'unknown'
            CHECK(route_source_type IN ('imap','graph','direct','unknown'))""")
    # A legacy/missing route receipt says nothing about whether a caller was direct.
    conn.execute("""UPDATE support_email_effects
        SET route_source_type = (
            SELECT route.source_type FROM inbound_email_work AS route
            WHERE route.email_id = support_email_effects.email_id
              AND route.source_type IN ('imap','graph')
        )
        WHERE route_source_type = 'unknown'
          AND EXISTS (
            SELECT 1 FROM inbound_email_work AS route
            WHERE route.email_id = support_email_effects.email_id
              AND route.source_type IN ('imap','graph')
          )""")
    conn.commit()


# Released v2-v133 migrations are registered explicitly after their definitions.
# v24 has no released migration. v134-v143 run explicitly after reconciliation
# in run_migrations; do not infer they are missing from this registry. Reconcile
# entries preserve idempotent repairs even on a current-stamped DB.
_MIGRATION_STEPS = (
    MigrationStep(2, _migrate_v2, 'processed_emails columns'),
    MigrationStep(3, _migrate_v3, 'telegram persistence tables'),
    MigrationStep(4, _migrate_v4, 'schema alignment'),
    MigrationStep(5, _migrate_v5, 'contact suggestions'),
    MigrationStep(6, _migrate_v6, 'llm_traces prompt_hash + per_guard_results'),
    MigrationStep(7, _migrate_v7, 'internal contact fields'),
    MigrationStep(8, _migrate_v8, 'project_tier column'),
    MigrationStep(9, _migrate_v9, 'closing_checklist table'),
    MigrationStep(10, _migrate_v10, 'processed_emails thread cols + inbound_replies table'),
    MigrationStep(11, _migrate_v11, 'evidence_store CHECK constraint adds manual_note'),
    MigrationStep(12, _migrate_v12, 'operator_interaction_log table'),
    MigrationStep(13, _migrate_v13, 'operator_state notification_mode column'),
    MigrationStep(14, _migrate_v14, 'ai_feedback table + operator_state budget columns'),
    MigrationStep(15, _migrate_v15, 'support_tickets + ticket_evidence + resolution_patterns'),
    MigrationStep(16, _migrate_v16, 'indexes on resolution_patterns + telegram_sessions'),
    MigrationStep(17, _migrate_v17, 'support_tickets reply_draft columns'),
    MigrationStep(18, _migrate_v18, 'support_chat_messages table'),
    MigrationStep(19, _migrate_v19, 'support_tickets.ai_summary_json'),
    MigrationStep(20, _migrate_v20, 'processed_emails.ticket_id'),
    MigrationStep(21, _migrate_v21, 'support_tickets.thread_anchor'),
    MigrationStep(22, _migrate_v22, 'drafts.ticket_id column'),
    MigrationStep(23, _migrate_v23, 'learned_rules.updated_at column'),
    MigrationStep(25, _migrate_v25, 'project_corrections table'),
    MigrationStep(26, _migrate_v26, 'project_chat_messages table'),
    MigrationStep(27, _migrate_v27, 'session_timeout_minutes column'),
    MigrationStep(28, _migrate_v28, 'agent_notes + session_summaries FTS5'),
    MigrationStep(29, _migrate_v29, 'email knowledge mining columns + table'),
    MigrationStep(30, _migrate_v30, 'learned rules dedup + llm_traces.trace_metadata'),
    MigrationStep(31, _migrate_v31, 'agent_notes.note_type column'),
    MigrationStep(32, _migrate_v32, 'agent_notes expanded note_type CHECK'),
    MigrationStep(33, _migrate_v33, 'operator_state response_language column'),
    MigrationStep(34, _migrate_v34, 'drafts.scheduled_send_at column'),
    MigrationStep(35, _migrate_v35, 'digest_queue table'),
    MigrationStep(36, _migrate_v36, 'support_tickets outreach category'),
    MigrationStep(37, _migrate_v37, 'work_items supertype table + backfill'),
    MigrationStep(38, _migrate_v38, 'inbound_replies.requested_docs_json'),
    MigrationStep(39, _migrate_v39, 'internal_actions table'),
    MigrationStep(40, _migrate_v40, 'domain taxonomy + wiki_usage_log'),
    MigrationStep(41, _migrate_v41, 'email_knowledge_log.status column'),
    MigrationStep(42, _migrate_v42, 'operator_state.thinking_window_enabled column'),
    MigrationStep(43, _migrate_v43, 'draft_status_history table'),
    MigrationStep(44, _migrate_v44, 'strip vendor domains from client_email'),
    MigrationStep(45, _migrate_v45, 'operator_state agent harness toggles'),
    MigrationStep(46, _migrate_v46, 'operator_memory + thread_artifacts'),
    MigrationStep(47, _migrate_v47, 'learned_rules legacy active status normalize'),
    MigrationStep(48, _migrate_v48, 'specialist_sessions for resume_from'),
    MigrationStep(49, _migrate_v49, 'work_item status backfill for terminal entities'),
    MigrationStep(50, _migrate_v50, 'party graph: project_parties + contact_project_links'),
    MigrationStep(51, _migrate_v51, 'support_tickets.counterparty_org_id + backfill'),
    MigrationStep(52, _migrate_v52, 'action_intents governance table'),
    MigrationStep(53, _migrate_v53, 'personal work item types and details'),
    MigrationStep(54, _migrate_v54, 'assistant schedules and runs'),
    MigrationStep(55, _migrate_v55, 'personal brief snapshots'),
    MigrationStep(56, _migrate_v56, 'entity-scoped operator memory'),
    MigrationStep(57, _migrate_v57, 'inbox intelligence insights'),
    MigrationStep(58, _migrate_v58, 'read-only calendar cache'),
    MigrationStep(59, _migrate_v59, 'meeting preparation snapshots'),
    MigrationStep(60, _migrate_v60, 'meeting-note ingestion'),
    MigrationStep(61, _migrate_v61, 'owned pending operations'),
    MigrationStep(62, _migrate_v62, 'crash-safe draft sending status'),
    MigrationStep(63, _migrate_v63, 'durable learning and retryable mining'),
    MigrationStep(64, _migrate_v64, 'proposed-update assessment metadata'),
    MigrationStep(65, _migrate_v65, 'immutable learning observations'),
    MigrationStep(66, _migrate_v66, 'observation-bound durable learning outbox'),
    MigrationStep(67, _migrate_v67, 'versioned memory candidate proposals'),
    MigrationStep(68, _migrate_v68, 'deterministic candidate governance'),
    MigrationStep(69, _migrate_v69, 'candidate deduplication and conflict records'),
    MigrationStep(70, _migrate_v70, 'append-only conflict lifecycle events'),
    MigrationStep(71, _migrate_v71, 'candidate evaluation records'),
    MigrationStep(72, _migrate_v72, 'evaluation reproducibility hashes'),
    MigrationStep(73, _migrate_v73, 'memory decisions and facts'),
    MigrationStep(74, _migrate_v74, 'memory fact hardening'),
    MigrationStep(75, _migrate_v75, 'projection aggregate and legacy index cleanup'),
    MigrationStep(76, _migrate_v76, 'projection FK and head integrity'),
    MigrationStep(77, _migrate_v77, 'historical fact and recovery hardening'),
    MigrationStep(78, _migrate_v78, 'organization-scoped memory candidates'),
    MigrationStep(79, _migrate_v79, 'exact memory exposure and outcomes'),
    MigrationStep(80, _migrate_v80, 'exact artifact linkage and attribution scopes'),
    MigrationStep(81, _migrate_v81, 'strict fact outcome invariants', reconcile=True),
    MigrationStep(82, _migrate_v82, 'governed memory lifecycle'),
    MigrationStep(83, _migrate_v83, 'lifecycle policy and rollback operations', reconcile=True),
    MigrationStep(84, _migrate_v84, 'governed validity and rollback event ledger'),
    MigrationStep(85, _migrate_v85, 'immutable lifecycle policy integrity'),
    MigrationStep(86, _migrate_v86, 'crash-safe lifecycle schema reconciliation', reconcile=True),
    MigrationStep(87, _migrate_v87, 'governed Wiki publication v2', reconcile=True),
    MigrationStep(88, _migrate_v88,
                  'durable Wiki publication claims and proposal binding', reconcile=True),
    MigrationStep(89, _migrate_v89, 'learning merge and conflict audit', reconcile=True),
    MigrationStep(90, _migrate_v90,
                  'learning canary, parity, and ingress receipts', reconcile=True),
    MigrationStep(91, _migrate_v91, 'audited legacy learning retirement', reconcile=True),
    MigrationStep(92, _migrate_v92, 'durable WP14 recovery controls', reconcile=True),
    MigrationStep(93, _migrate_v93, 'run-oriented encrypted tracing'),
    MigrationStep(94, _migrate_v94, 'canonical FIX session registry', reconcile=True),
    MigrationStep(95, _migrate_v95, 'Dashboard identity and session security', reconcile=True),
    MigrationStep(96, _migrate_v96, 'local Dashboard user authentication', reconcile=True),
    MigrationStep(97, _migrate_v97, 'repair draft sending status constraint', reconcile=True),
    MigrationStep(98, _migrate_v98, 'processed email timestamps and recipients', reconcile=True),
    MigrationStep(99, _migrate_v99, 'secure draft workspace and durable outbox', reconcile=True),
    MigrationStep(100, _migrate_v100, 'stable web-chat turn identities', reconcile=True),
    MigrationStep(101, _migrate_v101,
                  'AI Workspace threads and project chat backfill', reconcile=True),
    MigrationStep(102, _migrate_v102, 'evidence-backed project closing', reconcile=True),
    MigrationStep(103, _migrate_v103, 'asynchronous engineering trace pipeline', reconcile=True),
    MigrationStep(104, _migrate_v104, 'durable AI feedback trace linkage', reconcile=True),
    MigrationStep(105, _migrate_v105, 'Email Project Truth B shadow observations', reconcile=True),
    MigrationStep(106, _migrate_v106, 'durable email project reconciliation', reconcile=True),
    MigrationStep(107, _migrate_v107, 'superseded reconciliation phase', reconcile=True),
    MigrationStep(108, _migrate_v108, 'team identity columns', reconcile=True),
    MigrationStep(109, _migrate_v109, 'personal work item parent types', reconcile=True),
    MigrationStep(110, _migrate_v110, 'wiki compiler queue', reconcile=True),
    MigrationStep(111, _migrate_v111, 'durable specialist checkpoints', reconcile=True),
    MigrationStep(112, _migrate_v112, 'committed update notification outbox', reconcile=True),
    MigrationStep(113, _migrate_v113,
                  'followup reminder and action revision fields', reconcile=True),
    MigrationStep(114, _migrate_v114, reconcile=True),
    MigrationStep(115, _migrate_v115, reconcile=True),
    MigrationStep(116, _migrate_v116, reconcile=True),
    MigrationStep(117, _migrate_v117, reconcile=True),
    MigrationStep(118, _migrate_v118, reconcile=True),
    MigrationStep(119, _migrate_v119, reconcile=True),
    MigrationStep(120, _migrate_v120, reconcile=True),
    MigrationStep(121, _migrate_v121, reconcile=True),
    MigrationStep(122, _migrate_v122, reconcile=True),
    MigrationStep(123, _migrate_v123, reconcile=True),
    MigrationStep(124, _migrate_v124, reconcile=True),
    MigrationStep(125, _migrate_v125, reconcile=True),
    MigrationStep(126, _migrate_v126, reconcile=True),
    MigrationStep(127, _migrate_v127, reconcile=True),
    MigrationStep(128, _migrate_v128, reconcile=True),
    MigrationStep(129, _migrate_v129, reconcile=True),
    MigrationStep(130, _migrate_v130, reconcile=True),
    MigrationStep(131, _migrate_v131, reconcile=True),
    MigrationStep(132, _migrate_v132, reconcile=True),
    MigrationStep(133, _migrate_v133),
)
