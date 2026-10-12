"""Independent, retryable compatibility projection deliveries."""
from __future__ import annotations

import json
from datetime import datetime, timezone

from config import defaults
from src.db.connection import scoped_read, scoped_write
from src.repositories.memory_fact_repo import get_fact

_FLAGS = {"learned_rules": "LEARNING_PROJECT_LEARNED_RULES_PROJECTION", "response_pattern": "LEARNING_RESPONSE_PATTERN_PROJECTION", "operator_memory": "LEARNING_OPERATOR_MEMORY_PROJECTION"}


def process_projection_outbox(*, max_deliveries: int = 50, at=None) -> int:
    completed_at = at or datetime.now(timezone.utc)
    if completed_at.tzinfo is None or completed_at.utcoffset() is None:
        raise ValueError("projection time must be timezone-aware")
    completed_iso = completed_at.astimezone(timezone.utc).isoformat()
    with scoped_read() as conn:
        rows = conn.execute("""SELECT d.event_id,d.target,o.fact_id,o.event_type FROM memory_projection_deliveries d
            JOIN memory_projection_outbox o USING(event_id) WHERE d.status='pending' ORDER BY o.created_at,d.target LIMIT ?""", (max_deliveries,)).fetchall()
    processed = 0
    for row in rows:
        # Rollback/delete must propagate to every previously owned target even
        # when forward projection has since been disabled.
        if row["event_type"] != "delete" and not getattr(defaults, _FLAGS[row["target"]]):
            continue
        try:
            with scoped_write() as conn:
                delivery = conn.execute("SELECT status FROM memory_projection_deliveries WHERE event_id=? AND target=?", (row["event_id"], row["target"])).fetchone()
                if not delivery or delivery[0] != "pending": continue
                fact = get_fact(conn, row["fact_id"])
                _deliver(conn, row["target"], row["event_type"], fact)
                conn.execute("UPDATE memory_projection_deliveries SET status='completed',completed_at=?,last_error=NULL WHERE event_id=? AND target=?", (completed_iso, row["event_id"], row["target"]))
                _recompute_outbox_status(conn, row["event_id"], completed_iso)
            processed += 1
        except Exception as exc:
            with scoped_write() as conn:
                conn.execute("UPDATE memory_projection_deliveries SET attempts=attempts+1,last_error=?,status=CASE WHEN attempts+1>=5 THEN 'dead_letter' ELSE 'pending' END WHERE event_id=? AND target=?", (str(exc), row["event_id"], row["target"]))
                _recompute_outbox_status(conn, row["event_id"], completed_iso)
    return processed



def _recompute_outbox_status(conn, event_id, completed_iso=None):
    statuses = [row[0] for row in conn.execute("SELECT status FROM memory_projection_deliveries WHERE event_id=?", (event_id,)).fetchall()]
    if statuses and all(value == "completed" for value in statuses):
        status, completed = "completed", completed_iso
    elif statuses and all(value == "dead_letter" for value in statuses):
        status, completed = "dead_letter", None
    elif "dead_letter" in statuses:
        status, completed = "partial_failed", None
    else:
        status, completed = "pending", None
    conn.execute("UPDATE memory_projection_outbox SET status=?,completed_at=? WHERE event_id=?", (status, completed, event_id))


def _deliver(conn, target, event_type, fact):
    from src.learning.legacy_retirement import authorized_compatibility_projection
    store = "response_pattern_memory" if target == "response_pattern" else target
    with authorized_compatibility_projection(store):
        if event_type == "delete":
            return _remove_owned(conn, target, fact)
        from src.memory.projection_authority import fact_is_current
        if not fact_is_current(conn, fact.fact_id, fact.version):
            return
        {"learned_rules": _learned_rule, "response_pattern": _response_pattern, "operator_memory": _operator_memory}[target](conn, fact)


def _own(conn, target, key, fact, snapshot=None):
    conn.execute("INSERT INTO memory_projection_ownership(target,legacy_key,fact_id,fact_version,snapshot_json) VALUES (?,?,?,?,?) ON CONFLICT(target,legacy_key) DO UPDATE SET fact_id=excluded.fact_id,fact_version=excluded.fact_version,snapshot_json=excluded.snapshot_json", (target, key, fact.fact_id, fact.version, json.dumps(snapshot) if snapshot else None))


def _learned_rule(conn, fact):
    key=f"v2:{fact.fact_key}"
    current = conn.execute("SELECT 1 FROM learned_rules WHERE rule_id=?", (key,)).fetchone()
    owner = conn.execute("SELECT fact_id FROM memory_projection_ownership WHERE target='learned_rules' AND legacy_key=?", (key,)).fetchone()
    if current and not owner: return
    conn.execute("""INSERT INTO learned_rules(rule_id,created_at,trigger_type,source_project_id,condition,rule,extra_knowledge,scope,applicable_to,status,confidence)
        VALUES (?,?,'procedure',?,?,?,'{}',?,'["*"]','approved',1.0) ON CONFLICT(rule_id) DO UPDATE SET rule=excluded.rule,status='approved'""", (key,fact.valid_from.isoformat(),fact.scope_id if fact.scope_type=='project' else None,fact.predicate,json.dumps(fact.value,ensure_ascii=False,sort_keys=True),fact.scope_type))
    _own(conn,"learned_rules",key,fact)


def _response_pattern(conn, fact):
    if fact.target_type != "preference" or fact.subject_type != "contact" or not fact.subject_id:
        return
    project_id = fact.scope_id if fact.scope_type == "project" else None
    key = f"{fact.subject_id}|{project_id or '*'}"
    ownership = conn.execute(
        "SELECT snapshot_json FROM memory_projection_ownership "
        "WHERE target='response_pattern' AND legacy_key=?", (key,),
    ).fetchone()
    value = fact.value if isinstance(fact.value, dict) else {}
    tone = value.get("tone", "semi-formal")
    if ownership:
        row_id = json.loads(ownership[0] or "{}").get("row_id")
        updated = conn.execute(
            "UPDATE response_pattern_memory SET tone=?,source='learning_v2',confidence=1.0,"
            "updated_at=datetime('now') WHERE id=? AND source='learning_v2'", (tone, row_id),
        )
        if updated.rowcount != 1:
            raise RuntimeError("owned response-pattern row missing")
    else:
        current = conn.execute(
            "SELECT id FROM response_pattern_memory WHERE contact_id=? AND project_id IS ? LIMIT 1",
            (fact.subject_id, project_id),
        ).fetchone()
        if current:
            return
        cursor = conn.execute(
            "INSERT INTO response_pattern_memory(contact_id,project_id,tone,source,confidence) "
            "VALUES (?,?,?,'learning_v2',1.0)", (fact.subject_id, project_id, tone),
        )
        row_id = cursor.lastrowid
    _own(conn, "response_pattern", key, fact, {"row_id": row_id})


def _operator_memory(conn, fact):
    key=f"{fact.operator_id}|{fact.scope_type}|{fact.scope_id or '*'}|semantic|{fact.predicate}"; owner=conn.execute("SELECT fact_id FROM memory_projection_ownership WHERE target='operator_memory' AND legacy_key=?",(key,)).fetchone()
    current=conn.execute("SELECT 1 FROM operator_memory WHERE operator_id=? AND scope=? AND scope_id=? AND memory_type='semantic' AND memory_key=?",(fact.operator_id,fact.scope_type,fact.scope_id or '*',fact.predicate)).fetchone()
    if current and not owner: return
    conn.execute("""INSERT INTO operator_memory(operator_id,scope,scope_id,memory_type,memory_key,memory_value,source,active) VALUES (?,?,?,'semantic',?,?,'migration',1)
        ON CONFLICT(operator_id,scope,scope_id,memory_type,memory_key) DO UPDATE SET memory_value=excluded.memory_value,active=1,updated_at=datetime('now')""",(fact.operator_id,fact.scope_type,fact.scope_id or '*',fact.predicate,json.dumps(fact.value,ensure_ascii=False,sort_keys=True)))
    _own(conn,"operator_memory",key,fact)


def _remove_owned(conn,target,fact):
    rows=conn.execute("SELECT legacy_key FROM memory_projection_ownership WHERE target=? AND fact_id=?",(target,fact.fact_id)).fetchall()
    for row in rows:
        key=row[0]
        if target=='learned_rules': conn.execute("DELETE FROM learned_rules WHERE rule_id=?",(key,))
        elif target == 'response_pattern':
            ownership = conn.execute(
                "SELECT snapshot_json FROM memory_projection_ownership "
                "WHERE target='response_pattern' AND legacy_key=?", (key,),
            ).fetchone()
            row_id = json.loads(ownership[0] or "{}").get("row_id") if ownership else None
            conn.execute(
                "DELETE FROM response_pattern_memory WHERE id=? AND source='learning_v2'", (row_id,),
            )
        else:
            operator,scope,scope_id,kind,memory_key=key.split('|',4); conn.execute("DELETE FROM operator_memory WHERE operator_id=? AND scope=? AND scope_id=? AND memory_type=? AND memory_key=? AND source='migration'",(operator,scope,scope_id,kind,memory_key))
        conn.execute("DELETE FROM memory_projection_ownership WHERE target=? AND legacy_key=? AND fact_id=?",(target,key,fact.fact_id))
