"""Crash-safe repair for the historical v36 support-ticket table shape."""

import re
import sqlite3


_TICKET_COLUMNS = (
    "ticket_id", "title", "category", "severity", "status", "source_type",
    "related_project_id", "counterparty", "reporter_email", "reporter_contact_id",
    "description", "root_cause", "resolution", "assigned_team", "escalated_to",
    "reported_at", "resolved_at", "closed_at", "sla_target_hours",
    "resolution_pattern_id", "created_at", "updated_at", "reply_draft_text",
    "draft_generated_at", "ai_summary_json", "thread_anchor", "work_item_id",
    "counterparty_org_id",
)

# This is a snapshot of the current support_tickets contract. Do not change v36:
# a released migration must remain reproducible for legacy upgrades.
_REPAIRED_DDL = """
CREATE TABLE support_tickets_v134 (
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
    source_type TEXT NOT NULL DEFAULT 'email' CHECK (source_type IN (
        'email','telegram','manual'
    )),
    related_project_id TEXT REFERENCES projects(project_id),
    counterparty TEXT,
    counterparty_org_id TEXT REFERENCES organisations(org_id),
    reporter_email TEXT,
    reporter_contact_id TEXT REFERENCES contacts(contact_id),
    description TEXT,
    root_cause TEXT,
    resolution TEXT,
    assigned_team TEXT,
    escalated_to TEXT,
    reported_at TEXT NOT NULL DEFAULT (datetime('now')),
    resolved_at TEXT,
    closed_at TEXT,
    sla_target_hours REAL,
    resolution_pattern_id TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at TEXT NOT NULL DEFAULT (datetime('now')),
    reply_draft_text TEXT,
    draft_generated_at TEXT,
    ai_summary_json TEXT,
    thread_anchor TEXT,
    work_item_id TEXT
)
"""


def _needs_repair(conn: sqlite3.Connection) -> bool:
    columns = {row[1]: row for row in conn.execute("PRAGMA table_info(support_tickets)")}
    if not columns:
        raise RuntimeError("support_tickets table missing; cannot repair")
    for column in ("reported_at", "created_at", "updated_at"):
        if column not in columns or (columns[column][2:5]) != (
            "TEXT", 1, "datetime('now')"
        ):
            return True
    source = columns.get("source_type")
    if source is None or source[2:5] != ("TEXT", 1, "'email'"):
        return True
    foreign_keys = {
        (row[3], row[2], row[4])
        for row in conn.execute("PRAGMA foreign_key_list(support_tickets)")
    }
    if not {
        ("related_project_id", "projects", "project_id"),
        ("reporter_contact_id", "contacts", "contact_id"),
        ("counterparty_org_id", "organisations", "org_id"),
    } <= foreign_keys:
        return True
    sql = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='support_tickets'"
    ).fetchone()[0]
    match = re.search(
        r"\bCHECK\s*\(\s*source_type\s+IN\s*\(([^)]*)\)\s*\)",
        sql, flags=re.IGNORECASE,
    )
    return match is None or set(re.findall(r"'([^']+)'", match.group(1))) != {
        "email", "telegram", "manual"
    }


def repair_support_tickets(conn: sqlite3.Connection) -> bool:
    """Repair only drifted tables, rejecting bad legacy data without mutating it.

    The replacement and every dependent index/trigger are one transaction. Foreign
    keys must be disabled *before* BEGIN to let SQLite drop a referenced parent;
    foreign_key_check runs before commit and the previous setting is restored.
    """
    if not _needs_repair(conn):
        return False
    if conn.in_transaction:
        raise RuntimeError("support_tickets repair requires no open transaction")
    columns = {row[1] for row in conn.execute("PRAGMA table_info(support_tickets)")}
    if columns != set(_TICKET_COLUMNS):
        raise RuntimeError(
            "support_tickets has unexpected columns; manual review required "
            f"(missing={sorted(set(_TICKET_COLUMNS) - columns)}, "
            f"extra={sorted(columns - set(_TICKET_COLUMNS))})"
        )
    dependent_ddl = [
        row[0] for row in conn.execute(
            "SELECT sql FROM sqlite_master WHERE tbl_name='support_tickets' "
            "AND type IN ('index','trigger') AND sql IS NOT NULL ORDER BY type,name"
        )
    ]
    foreign_keys = conn.execute("PRAGMA foreign_keys").fetchone()[0]
    conn.execute("PRAGMA foreign_keys=OFF")
    try:
        conn.execute("BEGIN IMMEDIATE")
        conn.execute(_REPAIRED_DDL)
        names = ",".join(_TICKET_COLUMNS)
        conn.execute(
            f"INSERT INTO support_tickets_v134 ({names}) "
            f"SELECT {names} FROM support_tickets"
        )
        before = conn.execute("SELECT COUNT(*) FROM support_tickets").fetchone()[0]
        after = conn.execute("SELECT COUNT(*) FROM support_tickets_v134").fetchone()[0]
        if before != after:
            raise RuntimeError("support_tickets repair row count mismatch")
        conn.execute("DROP TABLE support_tickets")
        conn.execute("ALTER TABLE support_tickets_v134 RENAME TO support_tickets")
        for statement in dependent_ddl:
            conn.execute(statement)
        violations = conn.execute("PRAGMA foreign_key_check").fetchall()
        if violations:
            raise RuntimeError(
                "support_tickets repair found foreign-key violations; "
                f"manual review required: {violations[:5]}"
            )
        conn.commit()
    except BaseException:
        conn.rollback()
        raise
    finally:
        conn.execute(f"PRAGMA foreign_keys={foreign_keys}")
    return True
