"""WP11 lifecycle schema creation and crash-safe reconciliation."""
from __future__ import annotations

import hashlib
import json
import sqlite3
from collections.abc import Iterable


LIFECYCLE_TABLES = (
    "memory_lifecycle_policies",
    "memory_fact_lifecycle_policy",
    "memory_fact_validity_events",
    "memory_lifecycle_events",
    "memory_lifecycle_reviews",
    "memory_lifecycle_review_events",
    "memory_lifecycle_review_evidence",
    "memory_authority_legacy_quarantine",
    "memory_authority_denylist",
    "memory_lifecycle_operations",
    "memory_lifecycle_operation_events",
    "memory_rollback_targets",
    "memory_rollback_attempts",
)

STAGING_SUFFIXES = ("_legacy_v84", "_legacy_v85", "_v86_old", "_v86_new")
NEW_SUFFIX = "_v86_new"
OLD_SUFFIX = "_v86_old"

TABLE_DDL = """
CREATE TABLE IF NOT EXISTS memory_lifecycle_policies (
 policy_version TEXT NOT NULL,target_type TEXT NOT NULL CHECK(target_type IN ('semantic_fact','preference','procedure','wiki_publication')),
 default_ttl_seconds INTEGER NOT NULL CHECK(default_ttl_seconds>0),review_interval_seconds INTEGER NOT NULL CHECK(review_interval_seconds>0),
 revalidation_window_seconds INTEGER NOT NULL CHECK(revalidation_window_seconds>0),max_revalidation_horizon_seconds INTEGER CHECK(max_revalidation_horizon_seconds>0),
 expiry_required INTEGER NOT NULL CHECK(expiry_required IN (0,1)),negative_review_threshold INTEGER NOT NULL CHECK(negative_review_threshold>0),
 high_risk_behavior TEXT NOT NULL CHECK(high_risk_behavior IN ('quarantine_pending_review','remain_active_with_warning')),
 effective_from TEXT NOT NULL,effective_to TEXT,policy_hash TEXT NOT NULL,PRIMARY KEY(policy_version,target_type));
CREATE TABLE IF NOT EXISTS memory_fact_lifecycle_policy (
 fact_id TEXT PRIMARY KEY REFERENCES memory_facts(fact_id),policy_version TEXT NOT NULL,target_type TEXT NOT NULL,
 effective_from TEXT NOT NULL,effective_to TEXT,approved_valid_to TEXT NOT NULL,
 FOREIGN KEY(policy_version,target_type) REFERENCES memory_lifecycle_policies(policy_version,target_type));
CREATE TABLE IF NOT EXISTS memory_fact_validity_events (
 event_id TEXT PRIMARY KEY,fact_id TEXT NOT NULL REFERENCES memory_facts(fact_id),event_type TEXT NOT NULL CHECK(event_type IN ('approved','revalidated')),
 valid_to TEXT NOT NULL,next_review_at TEXT NOT NULL,actor_id TEXT NOT NULL,actor_role TEXT NOT NULL,reason TEXT NOT NULL,
 source_outcome_ids_json TEXT NOT NULL DEFAULT '[]',effective_at TEXT NOT NULL,idempotency_key TEXT NOT NULL UNIQUE,created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS memory_lifecycle_events (
 event_id TEXT PRIMARY KEY,fact_id TEXT NOT NULL REFERENCES memory_facts(fact_id),
 event_type TEXT NOT NULL CHECK(event_type IN ('expired','review_required','revalidated','review_resolved','rollback_requested')),
 reason_code TEXT NOT NULL,source_outcome_id TEXT,effective_at TEXT NOT NULL,idempotency_key TEXT NOT NULL UNIQUE,
 payload_json TEXT NOT NULL DEFAULT '{}',created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS memory_lifecycle_reviews (
 review_id TEXT PRIMARY KEY,fact_id TEXT NOT NULL REFERENCES memory_facts(fact_id),fact_version INTEGER NOT NULL,
 generation INTEGER NOT NULL CHECK(generation>0),reason_code TEXT NOT NULL,evidence_hash TEXT NOT NULL,evidence_json TEXT NOT NULL,opened_at TEXT NOT NULL,
 UNIQUE(fact_id,fact_version,reason_code,evidence_hash));
CREATE TABLE IF NOT EXISTS memory_lifecycle_review_events (
 event_id TEXT PRIMARY KEY,review_id TEXT NOT NULL REFERENCES memory_lifecycle_reviews(review_id),
 status TEXT NOT NULL CHECK(status IN ('pending','approved','revalidated','quarantined','revoked','superseded')),
 actor_id TEXT NOT NULL,reason TEXT NOT NULL,effective_at TEXT NOT NULL,idempotency_key TEXT NOT NULL UNIQUE,
 payload_json TEXT NOT NULL DEFAULT '{}',created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS memory_lifecycle_review_evidence (
 event_id TEXT PRIMARY KEY,review_id TEXT NOT NULL REFERENCES memory_lifecycle_reviews(review_id),
 outcome_id TEXT NOT NULL,effective_at TEXT NOT NULL,idempotency_key TEXT NOT NULL UNIQUE,created_at TEXT NOT NULL,
 UNIQUE(review_id,outcome_id));
CREATE TABLE IF NOT EXISTS memory_authority_legacy_quarantine (
 quarantine_id TEXT PRIMARY KEY,selector_kind TEXT NOT NULL CHECK(selector_kind='legacy_source_type'),selector_value TEXT NOT NULL,
 status TEXT NOT NULL CHECK(status='quarantined'),operation_id TEXT,actor_id TEXT NOT NULL,actor_role TEXT NOT NULL,reason TEXT NOT NULL,
 effective_at TEXT NOT NULL,created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS memory_authority_denylist (
 deny_id TEXT PRIMARY KEY,authority_kind TEXT NOT NULL CHECK(authority_kind IN ('source_id','content_hash','batch_id')),
 authority_value TEXT NOT NULL,operation_id TEXT NOT NULL,actor_id TEXT NOT NULL,actor_role TEXT NOT NULL,
 reason TEXT NOT NULL,effective_at TEXT NOT NULL,created_at TEXT NOT NULL,UNIQUE(authority_kind,authority_value));
CREATE TABLE IF NOT EXISTS memory_lifecycle_operations (
 operation_id TEXT PRIMARY KEY,operation_type TEXT NOT NULL CHECK(operation_type IN ('emergency_revoke','expiry','review_resolution','rollback','supersession')),
 idempotency_key TEXT NOT NULL UNIQUE,request_hash TEXT NOT NULL,actor_id TEXT NOT NULL,actor_role TEXT NOT NULL,
 reason TEXT NOT NULL,selector_kind TEXT,selector_value TEXT,started_at TEXT NOT NULL,effective_at TEXT NOT NULL,deadline TEXT NOT NULL,
 completed_at TEXT,status TEXT NOT NULL CHECK(status IN ('pending','completed','partial_failed','dead_letter','slo_breached')),
 result_json TEXT NOT NULL DEFAULT '{}',created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS memory_lifecycle_operation_events (
 event_id TEXT PRIMARY KEY,operation_id TEXT NOT NULL REFERENCES memory_lifecycle_operations(operation_id),
 status TEXT NOT NULL CHECK(status IN ('pending','completed','partial_failed','dead_letter','slo_breached')),
 reason TEXT NOT NULL,effective_at TEXT NOT NULL,payload_json TEXT NOT NULL DEFAULT '{}',idempotency_key TEXT NOT NULL UNIQUE,created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS memory_rollback_targets (
 operation_id TEXT NOT NULL REFERENCES memory_lifecycle_operations(operation_id),fact_id TEXT NOT NULL REFERENCES memory_facts(fact_id),
 target TEXT NOT NULL CHECK(target IN ('compatibility_projection','wiki_authority','wiki_index','context_cache')),
 status TEXT NOT NULL CHECK(status IN ('pending','completed','partial_failed','dead_letter','slo_breached','manual_review')),
 attempts INTEGER NOT NULL DEFAULT 0,last_error TEXT,completed_at TEXT,projection_event_id TEXT,ownership_snapshot_json TEXT,
 PRIMARY KEY(operation_id,fact_id,target));
CREATE TABLE IF NOT EXISTS memory_rollback_attempts (
 attempt_id TEXT PRIMARY KEY,operation_id TEXT NOT NULL,fact_id TEXT NOT NULL,target TEXT NOT NULL,attempt_number INTEGER NOT NULL,
 status TEXT NOT NULL CHECK(status IN ('completed','partial_failed','dead_letter')),error TEXT,started_at TEXT NOT NULL,completed_at TEXT NOT NULL,
 FOREIGN KEY(operation_id,fact_id,target) REFERENCES memory_rollback_targets(operation_id,fact_id,target),
 UNIQUE(operation_id,fact_id,target,attempt_number));
CREATE INDEX IF NOT EXISTS idx_memory_validity_fact ON memory_fact_validity_events(fact_id,effective_at);
CREATE INDEX IF NOT EXISTS idx_memory_lifecycle_events_fact ON memory_lifecycle_events(fact_id,effective_at);
CREATE INDEX IF NOT EXISTS idx_memory_review_events_review ON memory_lifecycle_review_events(review_id,effective_at);
CREATE INDEX IF NOT EXISTS idx_memory_rollback_status ON memory_rollback_targets(status,operation_id);
"""

TRIGGERS = {
    "trg_memory_lifecycle_policies_no_update": ("memory_lifecycle_policies", "UPDATE", "memory_lifecycle_policies is immutable"),
    "trg_memory_lifecycle_policies_no_delete": ("memory_lifecycle_policies", "DELETE", "memory_lifecycle_policies is immutable"),
    "trg_memory_fact_policy_no_update": ("memory_fact_lifecycle_policy", "UPDATE", "memory_fact_lifecycle_policy is immutable"),
    "trg_memory_fact_policy_no_delete": ("memory_fact_lifecycle_policy", "DELETE", "memory_fact_lifecycle_policy is immutable"),
    "trg_memory_validity_no_update": ("memory_fact_validity_events", "UPDATE", "memory_fact_validity_events is append-only"),
    "trg_memory_validity_no_delete": ("memory_fact_validity_events", "DELETE", "memory_fact_validity_events is append-only"),
    "trg_memory_lifecycle_events_no_update": ("memory_lifecycle_events", "UPDATE", "memory_lifecycle_events is append-only"),
    "trg_memory_lifecycle_events_no_delete": ("memory_lifecycle_events", "DELETE", "memory_lifecycle_events is append-only"),
    "trg_memory_reviews_no_update": ("memory_lifecycle_reviews", "UPDATE", "memory_lifecycle_reviews is append-only"),
    "trg_memory_reviews_no_delete": ("memory_lifecycle_reviews", "DELETE", "memory_lifecycle_reviews is append-only"),
    "trg_memory_review_events_no_update": ("memory_lifecycle_review_events", "UPDATE", "memory_lifecycle_review_events is append-only"),
    "trg_memory_review_events_no_delete": ("memory_lifecycle_review_events", "DELETE", "memory_lifecycle_review_events is append-only"),
    "trg_memory_review_evidence_no_update": ("memory_lifecycle_review_evidence", "UPDATE", "memory_lifecycle_review_evidence is append-only"),
    "trg_memory_review_evidence_no_delete": ("memory_lifecycle_review_evidence", "DELETE", "memory_lifecycle_review_evidence is append-only"),
    "trg_memory_legacy_quarantine_no_update": ("memory_authority_legacy_quarantine", "UPDATE", "memory_authority_legacy_quarantine is append-only"),
    "trg_memory_legacy_quarantine_no_delete": ("memory_authority_legacy_quarantine", "DELETE", "memory_authority_legacy_quarantine is append-only"),
    "trg_memory_authority_denylist_no_update": ("memory_authority_denylist", "UPDATE", "memory_authority_denylist is append-only"),
    "trg_memory_authority_denylist_no_delete": ("memory_authority_denylist", "DELETE", "memory_authority_denylist is append-only"),
    "trg_memory_operation_events_no_update": ("memory_lifecycle_operation_events", "UPDATE", "memory_lifecycle_operation_events is append-only"),
    "trg_memory_operation_events_no_delete": ("memory_lifecycle_operation_events", "DELETE", "memory_lifecycle_operation_events is append-only"),
    "trg_memory_rollback_attempts_no_update": ("memory_rollback_attempts", "UPDATE", "memory_rollback_attempts is append-only"),
    "trg_memory_rollback_attempts_no_delete": ("memory_rollback_attempts", "DELETE", "memory_rollback_attempts is append-only"),
    "trg_memory_rollback_targets_no_delete": ("memory_rollback_targets", "DELETE", "memory_rollback_targets cannot be deleted"),
    "trg_memory_operations_no_delete": ("memory_lifecycle_operations", "DELETE", "memory_lifecycle_operations cannot be deleted"),
}


def _checkpoint(_phase: str) -> None:
    """Monkeypatchable crash checkpoint used by migration recovery tests."""


def _ddl_statements() -> tuple[str, ...]:
    """Return individual table/index DDL without executescript transactions."""
    return tuple(statement.strip() for statement in TABLE_DDL.split(";") if statement.strip())


def _new_table_statement(statement: str) -> str:
    result = statement
    for table in sorted(LIFECYCLE_TABLES, key=len, reverse=True):
        result = result.replace(table, table + NEW_SUFFIX)
    return result


def _create_schema(conn: sqlite3.Connection, *, staged: bool = False) -> None:
    for statement in _ddl_statements():
        conn.execute(_new_table_statement(statement) if staged else statement)


def _table_exists(conn: sqlite3.Connection, name: str) -> bool:
    return conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)).fetchone() is not None


def _columns(conn: sqlite3.Connection, table: str) -> tuple[str, ...]:
    return tuple(row[1] for row in conn.execute(f'PRAGMA table_info("{table}")'))


def _rows(conn: sqlite3.Connection, table: str) -> list[dict]:
    cursor = conn.execute(f'SELECT * FROM "{table}"')
    names = tuple(item[0] for item in cursor.description)
    return [dict(zip(names, row)) for row in cursor.fetchall()]


def _value(row: dict, *names: str, default=None):
    return next((row[name] for name in names if row.get(name) is not None), default)


def _stable_id(prefix: str, *parts: object) -> str:
    raw = "|".join(str(part or "") for part in parts).encode()
    return f"{prefix}:{hashlib.sha256(raw).hexdigest()[:24]}"


def _policy_row(row: dict) -> dict:
    from src.learning.lifecycle_policy import LifecyclePolicy, policy_hash
    policy = LifecyclePolicy(
        policy_version=row["policy_version"], target_type=row["target_type"],
        default_ttl_seconds=row["default_ttl_seconds"], review_interval_seconds=row["review_interval_seconds"],
        revalidation_window_seconds=row["revalidation_window_seconds"],
        max_revalidation_horizon_seconds=row.get("max_revalidation_horizon_seconds"),
        expiry_required=bool(row["expiry_required"]), negative_review_threshold=row["negative_review_threshold"],
        high_risk_behavior=row["high_risk_behavior"], effective_from=row["effective_from"], effective_to=row.get("effective_to"),
    )
    result = dict(row)
    result["max_revalidation_horizon_seconds"] = policy.max_revalidation_horizon_seconds
    result["policy_hash"] = policy_hash(policy)
    return result


def _mapped_rows(table: str, rows: Iterable[dict]) -> tuple[list[dict], list[dict]]:
    mapped: list[dict] = []
    quarantined: list[dict] = []
    for original in rows:
        row = dict(original)
        if table == "memory_lifecycle_policies":
            row = _policy_row(row)
        elif table == "memory_lifecycle_reviews":
            row.setdefault("generation", 1)
            row.setdefault("evidence_hash", f"legacy:{row['review_id']}")
        elif table == "memory_lifecycle_operations":
            row.setdefault("effective_at", row.get("started_at"))
        elif table == "memory_rollback_targets":
            if row.get("target") == "wiki_cache":
                row["target"] = "context_cache"
            row.setdefault("ownership_snapshot_json", None)
        elif table == "memory_authority_denylist":
            kind = str(_value(row, "authority_kind", "source_type", default="")).strip()
            aliases = {"batch": "batch_id", "batch_id": "batch_id", "source_id": "source_id", "content_hash": "content_hash"}
            value_names = {
                "source_id": ("authority_value", "source_id"), "batch_id": ("authority_value", "batch_id", "batch"),
                "batch": ("authority_value", "batch", "batch_id"), "content_hash": ("authority_value", "content_hash"),
            }
            value = str(_value(row, *value_names.get(kind, ()), default="")).strip()
            if kind not in aliases or not value:
                created = str(_value(row, "created_at", "effective_at", default="1970-01-01T00:00:00+00:00"))
                deny_id = str(_value(row, "deny_id", default=_stable_id("legacy-deny", kind, value, created)))
                quarantined.append({
                    "quarantine_id": deny_id, "selector_kind": "legacy_source_type",
                    "selector_value": str(_value(row, "source", "authority_value", default=value)), "status": "quarantined",
                    "operation_id": _value(row, "operation_id"), "actor_id": str(_value(row, "actor_id", default="migration")),
                    "actor_role": str(_value(row, "actor_role", default="system_lifecycle")),
                    "reason": str(_value(row, "reason", default="quarantined unsafe broad legacy source selector")),
                    "effective_at": str(_value(row, "effective_at", "created_at", default=created)), "created_at": created,
                })
                continue
            deny_id = str(_value(row, "deny_id", default=_stable_id("legacy-deny", aliases[kind], value)))
            created = str(_value(row, "created_at", "effective_at", default="1970-01-01T00:00:00+00:00"))
            row = {
                "deny_id": deny_id, "authority_kind": aliases[kind], "authority_value": value,
                "operation_id": str(_value(row, "operation_id", default=f"legacy:{deny_id}")),
                "actor_id": str(_value(row, "actor_id", default="migration")),
                "actor_role": str(_value(row, "actor_role", default="system_lifecycle")),
                "reason": str(_value(row, "reason", default="migrated legacy authority selector")),
                "effective_at": str(_value(row, "effective_at", "created_at", default=created)), "created_at": created,
            }
        mapped.append(row)
    return mapped, quarantined


def _insert_rows(conn: sqlite3.Connection, table: str, rows: Iterable[dict]) -> set[tuple]:
    columns = _columns(conn, table)
    info = sorted(conn.execute(f'PRAGMA table_info("{table}")'), key=lambda item: item[5])
    primary = tuple(item[1] for item in info if item[5])
    expected: set[tuple] = set()
    for row in rows:
        values = {name: row.get(name) for name in columns if name in row}
        if not values:
            continue
        names = tuple(values)
        sql = f'INSERT OR IGNORE INTO "{table}" ({",".join(names)}) VALUES ({",".join("?" for _ in names)})'
        conn.execute(sql, tuple(values[name] for name in names))
        expected.add(tuple(row.get(name) for name in primary))
    return expected


def _source_tables(conn: sqlite3.Connection) -> dict[str, list[str]]:
    return {
        table: [name for name in (table, *(table + suffix for suffix in STAGING_SUFFIXES)) if _table_exists(conn, name)]
        for table in LIFECYCLE_TABLES
    }


def _needs_rebuild(conn: sqlite3.Connection, sources: dict[str, list[str]]) -> bool:
    if any(any(name != table for name in names) for table, names in sources.items()):
        return True
    required = {
        "memory_lifecycle_policies": {"max_revalidation_horizon_seconds", "policy_hash"},
        "memory_lifecycle_reviews": {"generation", "evidence_hash"},
        "memory_lifecycle_operations": {"effective_at"},
        "memory_rollback_targets": {"ownership_snapshot_json"},
        "memory_authority_denylist": {"operation_id", "actor_role", "authority_value"},
    }
    if any(not _table_exists(conn, table) or not columns <= set(_columns(conn, table)) for table, columns in required.items()):
        return True
    deny_columns = set(_columns(conn, "memory_authority_denylist"))
    if deny_columns & {"source", "source_id", "batch", "batch_id", "content_hash"}:
        return True
    deny_sql = conn.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name='memory_authority_denylist'").fetchone()
    targets_sql = conn.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name='memory_rollback_targets'").fetchone()
    return bool(
        (deny_sql and "authority_kind IN ('source_id','content_hash','batch_id')" not in (deny_sql[0] or ""))
        or (targets_sql and ("context_cache" not in (targets_sql[0] or "") or "manual_review" not in (targets_sql[0] or "")))
    )


def _drop_triggers(conn: sqlite3.Connection) -> None:
    for name in TRIGGERS:
        conn.execute(f'DROP TRIGGER IF EXISTS "{name}"')


def _create_triggers(conn: sqlite3.Connection) -> None:
    _drop_triggers(conn)
    for name, (table, action, message) in TRIGGERS.items():
        conn.execute(
            f'CREATE TRIGGER "{name}" BEFORE {action} ON "{table}" '
            f"BEGIN SELECT RAISE(ABORT,{json.dumps(message)}); END"
        )
    attached = dict(conn.execute("SELECT name,tbl_name FROM sqlite_master WHERE type='trigger'"))
    missing = [name for name, (table, _, _) in TRIGGERS.items() if attached.get(name) != table]
    if missing:
        raise RuntimeError(f"lifecycle trigger attachment mismatch: {missing}")


def _validate_bindings(conn: sqlite3.Connection, suffix: str = "") -> None:
    binding = "memory_fact_lifecycle_policy" + suffix
    policies = "memory_lifecycle_policies" + suffix
    broken = conn.execute(f"""SELECT b.fact_id FROM "{binding}" b
        LEFT JOIN memory_facts f ON f.fact_id=b.fact_id
        LEFT JOIN "{policies}" p ON p.policy_version=b.policy_version AND p.target_type=b.target_type
        WHERE f.fact_id IS NULL OR p.policy_version IS NULL LIMIT 1""").fetchone()
    if broken:
        raise RuntimeError(f"invalid lifecycle policy binding: {broken[0]}")
    for row in _rows(conn, policies):
        if _policy_row(row)["policy_hash"] != row["policy_hash"]:
            raise RuntimeError(
                f"invalid lifecycle policy hash: {row['policy_version']}/{row['target_type']}"
            )


def _validate_keys(
    conn: sqlite3.Connection, expected: dict[str, set[tuple]], suffix: str,
) -> None:
    for table, keys in expected.items():
        target = table + suffix
        info = sorted(conn.execute(f'PRAGMA table_info("{target}")'), key=lambda item: item[5])
        primary = tuple(item[1] for item in info if item[5])
        actual = {
            tuple(row)
            for row in conn.execute(f'SELECT {",".join(primary)} FROM "{target}"')
        } if primary else set()
        if not keys <= actual:
            raise RuntimeError(f"lifecycle reconciliation lost keys for {table}: {keys - actual}")


def _foreign_key_violations(conn: sqlite3.Connection, suffix: str) -> list[tuple]:
    violations: list[tuple] = []
    for table in LIFECYCLE_TABLES:
        violations.extend(conn.execute(f'PRAGMA foreign_key_check("{table + suffix}")').fetchall())
    return violations


def _reconcile(conn: sqlite3.Connection, sources: dict[str, list[str]]) -> None:
    snapshots = {
        table: [row for source in names for row in _rows(conn, source)]
        for table, names in sources.items()
    }
    conn.commit()
    conn.execute("PRAGMA foreign_keys=OFF")
    expected: dict[str, set[tuple]] = {}
    try:
        conn.execute("BEGIN IMMEDIATE")
        for table in reversed(LIFECYCLE_TABLES):
            conn.execute(f'DROP TABLE IF EXISTS "{table + NEW_SUFFIX}"')
        _create_schema(conn, staged=True)
        _checkpoint("v86_new_tables_created")

        quarantine_rows: list[dict] = []
        midpoint = len(LIFECYCLE_TABLES) // 2
        for index, table in enumerate(LIFECYCLE_TABLES):
            mapped, quarantined = _mapped_rows(table, snapshots[table])
            quarantine_rows.extend(quarantined)
            expected[table] = _insert_rows(conn, table + NEW_SUFFIX, mapped)
            if index == midpoint:
                _checkpoint("v86_mid_copy")
        expected["memory_authority_legacy_quarantine"] |= _insert_rows(
            conn, "memory_authority_legacy_quarantine" + NEW_SUFFIX, quarantine_rows
        )
        _validate_keys(conn, expected, NEW_SUFFIX)
        violations = _foreign_key_violations(conn, NEW_SUFFIX)
        if violations:
            raise RuntimeError(f"lifecycle staged foreign-key failure: {violations[:3]}")
        _validate_bindings(conn, NEW_SUFFIX)
        _checkpoint("v86_pre_swap")

        _drop_triggers(conn)
        for index, table in enumerate(reversed(LIFECYCLE_TABLES)):
            old = table + OLD_SUFFIX
            conn.execute(f'DROP TABLE IF EXISTS "{old}"')
            if _table_exists(conn, table):
                conn.execute(f'ALTER TABLE "{table}" RENAME TO "{old}"')
            if index == 0:
                _checkpoint("v86_post_first_rename")
        for table in LIFECYCLE_TABLES:
            conn.execute(f'ALTER TABLE "{table + NEW_SUFFIX}" RENAME TO "{table}"')
        _checkpoint("v86_post_swap_pre_trigger")

        _checkpoint("v86_validation")
        _validate_keys(conn, expected, "")
        _validate_bindings(conn)
        violations = _foreign_key_violations(conn, "")
        if violations:
            raise RuntimeError(f"lifecycle canonical foreign-key failure: {violations[:3]}")
        _create_triggers(conn)

        for table in reversed(LIFECYCLE_TABLES):
            for suffix in (OLD_SUFFIX, "_legacy_v84", "_legacy_v85"):
                conn.execute(f'DROP TABLE IF EXISTS "{table + suffix}"')
        _validate_bindings(conn)
        if conn.execute("PRAGMA foreign_key_check").fetchall():
            raise RuntimeError("lifecycle foreign-key failure after staging cleanup")
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.execute("PRAGMA foreign_keys=ON")
        if conn.execute("PRAGMA foreign_keys").fetchone()[0] != 1:
            raise RuntimeError("lifecycle reconciliation failed to restore foreign keys")


def ensure_lifecycle_schema(conn: sqlite3.Connection) -> None:
    sources = _source_tables(conn)
    if _needs_rebuild(conn, sources):
        _reconcile(conn, sources)
    else:
        _create_schema(conn)
        _validate_bindings(conn)
        _create_triggers(conn)
        conn.commit()
