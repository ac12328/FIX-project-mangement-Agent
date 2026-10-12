"""Compatibility projections may not outlive their canonical authority."""
import json


def projection_is_current(conn, target, legacy_key):
    owner = conn.execute("SELECT fact_id,fact_version FROM memory_projection_ownership WHERE target=? AND legacy_key=?",
                         (target, legacy_key)).fetchone()
    if not owner:
        return True  # Independent legacy/curated data is not a governed projection.
    return fact_is_current(conn, owner[0], owner[1])


def fact_is_current(conn, fact_id, version):
    head = conn.execute("SELECT 1 FROM memory_fact_heads WHERE fact_id=? AND fact_version=?", (fact_id, version)).fetchone()
    state = conn.execute("SELECT state FROM memory_fact_state_events WHERE fact_id=? ORDER BY effective_at DESC,created_at DESC,event_seq DESC LIMIT 1",
                         (fact_id,)).fetchone()
    return bool(head and state and state[0] == "active")


def operator_projection_key(row):
    return f"{row['operator_id']}|{row['scope']}|{row['scope_id']}|{row['memory_type']}|{row['memory_key']}"


def operator_projection_is_current(conn, row):
    return projection_is_current(conn, "operator_memory", operator_projection_key(row))


def response_projection_is_current(conn, row):
    key = f"{row['contact_id']}|{row['project_id'] or '*'}"
    owner = conn.execute("SELECT fact_id,fact_version,snapshot_json FROM memory_projection_ownership WHERE target='response_pattern' AND legacy_key=?", (key,)).fetchone()
    if not owner:
        return row["source"] != "learning_v2"
    try:
        owned_id = json.loads(owner[2] or "{}").get("row_id")
    except (ValueError, TypeError, AttributeError):
        return False
    if owned_id != row["id"]:
        return row["source"] != "learning_v2"
    return fact_is_current(conn, owner[0], owner[1])
