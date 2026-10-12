"""Atomic canonical fact materialization primitives."""
from __future__ import annotations

import sqlite3
import uuid
from datetime import datetime, timedelta

from src.repositories.memory_fact_repo import MemoryFact, append_state, get_active_fact, insert_fact

TARGETS = ("learned_rules", "response_pattern", "operator_memory")


def fact_key(candidate: dict) -> str:
    return "|".join(str(item or "*") for item in (
        candidate["operator_id"], candidate["scope_type"], candidate.get("scope_id"),
        candidate["subject_type"], candidate.get("subject_id"), candidate["predicate"],
    ))


def enqueue_projection(conn: sqlite3.Connection, fact_id: str, event_type: str) -> str:
    event_id = f"mp_{uuid.uuid4().hex}"
    conn.execute("INSERT INTO memory_projection_outbox(event_id,fact_id,event_type,status) VALUES (?,?,?,'pending')", (event_id, fact_id, event_type))
    conn.executemany("INSERT INTO memory_projection_deliveries(event_id,target,status) VALUES (?,?,'pending')", [(event_id, target) for target in TARGETS])
    return event_id


def materialize(conn: sqlite3.Connection, candidate: dict, *, decision_id: str, evaluation_id: str | None,
                decided_at: datetime, actor_id: str, reason: str, expected_replaced_fact_id: str | None = None) -> MemoryFact:
    key = fact_key(candidate)
    active = get_active_fact(conn, key)
    if expected_replaced_fact_id is None and active:
        raise ValueError("active fact already exists; explicit supersession required")
    if expected_replaced_fact_id is not None and (active is None or active.fact_id != expected_replaced_fact_id):
        raise ValueError("supersession target does not match candidate fact key")
    version = active.version + 1 if active else conn.execute("SELECT COALESCE(MAX(fact_version),0)+1 FROM memory_facts WHERE fact_key=?", (key,)).fetchone()[0]
    rollback_operation_id = None
    if active:
        append_state(conn, active.fact_id, state="superseded", actor_id=actor_id, reason=reason, at=decided_at, event_id=f"mfs_{uuid.uuid4().hex}")
        conn.execute("DELETE FROM memory_fact_heads WHERE fact_key=? AND fact_id=?", (key, active.fact_id))
        projection_event_id = enqueue_projection(conn, active.fact_id, "delete")
        from src.learning.operation_service import create_operation
        from src.learning.rollback_service import create_rollback_work
        rollback_operation_id = f"mop_{uuid.uuid4().hex}"
        create_operation(conn, operation_id=rollback_operation_id, operation_type="supersession",
            idempotency_key=f"supersession:{decision_id}:{active.fact_id}", request_hash=decision_id,
            actor_id=actor_id, actor_role="memory_governor", reason=reason, effective_at=decided_at)
        create_rollback_work(conn, operation_id=rollback_operation_id, fact_id=active.fact_id,
                             projection_event_id=projection_event_id)
    provenance = {"operator_id": candidate["operator_id"], "observation_id": candidate["observation_id"],
                  "extractor_version": candidate["extractor_version"], "model_version": candidate["model_version"],
                  "prompt_version": candidate["prompt_version"], "schema_version": candidate["schema_version"]}
    from src.learning.lifecycle_policy import policy_for
    suggested_expiry = datetime.fromisoformat(candidate["suggested_expiry"]) if candidate["suggested_expiry"] else None
    policy = policy_for(candidate["target_type"])
    approved_valid_to = suggested_expiry or (decided_at + timedelta(seconds=policy.default_ttl_seconds))
    fact = MemoryFact(fact_id=f"mf_{uuid.uuid4().hex}", fact_key=key, version=version,
        operator_id=candidate["operator_id"], candidate_id=candidate["candidate_id"], candidate_version=candidate["candidate_version"],
        validation_id=candidate["validation_id"], decision_id=decision_id, evaluation_id=evaluation_id,
        target_type=candidate["target_type"], subject_type=candidate["subject_type"], subject_id=candidate.get("subject_id"),
        predicate=candidate["predicate"], value=candidate["proposed_value"], scope_type=candidate["scope_type"],
        scope_id=candidate.get("scope_id"), evidence_refs=candidate["evidence_refs"], provenance=provenance,
        valid_from=decided_at, approved_valid_to=approved_valid_to)
    insert_fact(conn, fact)
    from src.learning.lifecycle_policy import persist_policy_snapshot
    persist_policy_snapshot(conn, fact_id=fact.fact_id, target_type=fact.target_type,
                            valid_from=decided_at, approved_valid_to=approved_valid_to)
    append_state(conn, fact.fact_id, state="active", actor_id=actor_id, reason=reason, at=decided_at, event_id=f"mfs_{uuid.uuid4().hex}")
    conn.execute("INSERT INTO memory_fact_heads(fact_key,fact_id,fact_version) VALUES (?,?,?)", (key, fact.fact_id, version))
    enqueue_projection(conn, fact.fact_id, "upsert")
    return fact
