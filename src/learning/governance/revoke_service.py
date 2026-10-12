"""Shared transactional governed fact deactivation used by revoke and expiry."""
from __future__ import annotations

import json
import sqlite3
import uuid
from datetime import datetime
from typing import Literal

from src.memory.fact_store import enqueue_projection
from src.repositories.memory_decision_repo import MemoryDecision, insert_decision
from src.repositories.memory_fact_repo import append_state, get_active_fact, get_fact


def governed_deactivate(
    conn: sqlite3.Connection, *, fact_id: str, actor_id: str, reason: str,
    decided_at: datetime, decision_type: Literal["revoke", "expire"], operation_id: str | None = None,
) -> tuple[MemoryDecision | None, str | None]:
    fact = get_fact(conn, fact_id)
    active = get_active_fact(conn, fact.fact_key)
    if active is None or active.fact_id != fact_id:
        return None, None
    decision = MemoryDecision(
        decision_id=f"md_{uuid.uuid4().hex}", candidate_id=fact.candidate_id,
        candidate_version=fact.candidate_version, decision_type=decision_type,
        actor_id=actor_id, reason=reason, validation_id=fact.validation_id,
        evaluation_id=fact.evaluation_id, replaces_fact_id=fact_id,
        decision_payload_json=json.dumps({"operation_id": operation_id}, sort_keys=True),
        decided_at=decided_at,
    )
    insert_decision(conn, decision)
    append_state(conn, fact_id, state="revoked", actor_id=actor_id, reason=reason,
                 at=decided_at, event_id=f"mfs_{uuid.uuid4().hex}")
    deleted = conn.execute(
        "DELETE FROM memory_fact_heads WHERE fact_key=? AND fact_id=?", (fact.fact_key, fact_id),
    )
    if deleted.rowcount != 1:
        raise RuntimeError("fact activation changed during governed deactivation")
    projection_event_id = enqueue_projection(conn, fact_id, "delete")
    conn.execute(
        "INSERT INTO memory_audit_events(event_id,event_type,decision_id,fact_id,payload_json,created_at) VALUES (?,?,?,?,?,?)",
        (f"ma_{uuid.uuid4().hex}", f"memory_{decision_type}", decision.decision_id, fact_id,
         decision.decision_payload_json, decided_at.isoformat()),
    )
    return decision, projection_event_id
