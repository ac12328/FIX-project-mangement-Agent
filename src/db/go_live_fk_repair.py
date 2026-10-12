"""Repair the evidence foreign key damaged by an early evidence-table rename."""

import sqlite3


_COLUMNS = (
    "project_id", "actual_go_live", "evidence_id", "source_quote",
    "source_hash", "recorded_by", "recorded_at",
)

# The v11 evidence_store rename could retarget this later table's foreign key
# to evidence_store_old on a v1 upgrade with foreign keys enabled.
_REPLACEMENT_DDL = """
CREATE TABLE project_go_live_confirmations_v135 (
    project_id TEXT PRIMARY KEY REFERENCES projects(project_id),
    actual_go_live TEXT NOT NULL,
    evidence_id TEXT NOT NULL REFERENCES evidence_store(evidence_id),
    source_quote TEXT NOT NULL,
    source_hash TEXT NOT NULL,
    recorded_by TEXT NOT NULL,
    recorded_at TEXT NOT NULL DEFAULT (datetime('now'))
)
"""


def repair_go_live_evidence_fk(conn: sqlite3.Connection) -> bool:
    """Restore the fresh-schema FK atomically; never discard invalid rows."""
    columns = {row[1] for row in conn.execute(
        "PRAGMA table_info(project_go_live_confirmations)"
    )}
    if columns != set(_COLUMNS):
        raise RuntimeError(
            "project_go_live_confirmations columns differ from the supported "
            "contract; manual review required"
        )
    foreign_keys = {
        (row[3], row[2], row[4])
        for row in conn.execute(
            "PRAGMA foreign_key_list(project_go_live_confirmations)"
        )
    }
    expected = {
        ("project_id", "projects", "project_id"),
        ("evidence_id", "evidence_store", "evidence_id"),
    }
    if foreign_keys == expected:
        return False
    historical = {
        ("project_id", "projects", "project_id"),
        ("evidence_id", "evidence_store_old", "evidence_id"),
    }
    if foreign_keys != historical:
        raise RuntimeError(
            "project_go_live_confirmations has unexpected foreign keys; "
            "manual review required"
        )
    if conn.in_transaction:
        raise RuntimeError("go-live foreign-key repair requires no open transaction")
    dependent_ddl = [
        row[0] for row in conn.execute(
            "SELECT sql FROM sqlite_master "
            "WHERE tbl_name='project_go_live_confirmations' "
            "AND type IN ('index','trigger') AND sql IS NOT NULL ORDER BY type,name"
        )
    ]
    was_enabled = conn.execute("PRAGMA foreign_keys").fetchone()[0]
    conn.execute("PRAGMA foreign_keys=OFF")
    try:
        conn.execute("BEGIN IMMEDIATE")
        conn.execute(_REPLACEMENT_DDL)
        names = ",".join(_COLUMNS)
        conn.execute(
            f"INSERT INTO project_go_live_confirmations_v135 ({names}) "
            f"SELECT {names} FROM project_go_live_confirmations"
        )
        original_count = conn.execute(
            "SELECT COUNT(*) FROM project_go_live_confirmations"
        ).fetchone()[0]
        new_count = conn.execute(
            "SELECT COUNT(*) FROM project_go_live_confirmations_v135"
        ).fetchone()[0]
        if new_count != original_count:
            raise RuntimeError("go-live confirmation repair row count mismatch")
        conn.execute("DROP TABLE project_go_live_confirmations")
        conn.execute(
            "ALTER TABLE project_go_live_confirmations_v135 "
            "RENAME TO project_go_live_confirmations"
        )
        for statement in dependent_ddl:
            conn.execute(statement)
        violations = conn.execute("PRAGMA foreign_key_check").fetchall()
        if violations:
            raise RuntimeError(
                "go-live confirmation repair found foreign-key violations; "
                f"manual review required: {violations[:5]}"
            )
        conn.commit()
    except BaseException:
        conn.rollback()
        raise
    finally:
        conn.execute(f"PRAGMA foreign_keys={was_enabled}")
    return True
