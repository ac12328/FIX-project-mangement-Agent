"""Append-only canonical memory facts and derived current-state heads."""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict


class MemoryFact(BaseModel):
    model_config = ConfigDict(frozen=True)
    fact_id: str
    fact_key: str
    version: int
    operator_id: str
    candidate_id: str
    candidate_version: int
    validation_id: str
    decision_id: str
    evaluation_id: str | None = None
    target_type: str
    subject_type: str
    subject_id: str | None = None
    predicate: str
    value: Any
    scope_type: str
    scope_id: str | None = None
    evidence_refs: list[str]
    provenance: dict[str, Any]
    valid_from: datetime
    approved_valid_to: datetime | None = None
    state: Literal["active", "superseded", "revoked"] = "active"
    state_at: datetime | None = None

    @property
    def status(self): return self.state
    @property
    def valid_to(self): return self.state_at if self.state != "active" else self.approved_valid_to


def _load(conn: sqlite3.Connection, row) -> MemoryFact:
    value = dict(row)
    value["version"] = value.pop("fact_version")
    value["approved_valid_to"] = value.pop("valid_to")
    value["value"] = json.loads(value.pop("value_json"))
    value["evidence_refs"] = json.loads(value.pop("evidence_refs_json"))
    provenance = json.loads(value.pop("provenance_json"))
    value["provenance"] = provenance
    value["operator_id"] = provenance["operator_id"]
    event = conn.execute("SELECT state,effective_at FROM memory_fact_state_events WHERE fact_id=? ORDER BY effective_at DESC,created_at DESC,event_seq DESC LIMIT 1", (value["fact_id"],)).fetchone()
    value["state"] = event[0] if event else value.pop("status")
    value["state_at"] = event[1] if event and event[0] != "active" else None
    value.pop("status", None)
    return MemoryFact(**value)


def get_active_fact(conn: sqlite3.Connection, fact_key: str) -> MemoryFact | None:
    row = conn.execute("SELECT f.* FROM memory_fact_heads h JOIN memory_facts f ON f.fact_id=h.fact_id WHERE h.fact_key=?", (fact_key,)).fetchone()
    return _load(conn, row) if row else None


def get_fact(conn: sqlite3.Connection, fact_id: str) -> MemoryFact:
    row = conn.execute("SELECT * FROM memory_facts WHERE fact_id=?", (fact_id,)).fetchone()
    if not row: raise KeyError(fact_id)
    return _load(conn, row)


def list_fact_versions(conn: sqlite3.Connection, fact_key: str) -> list[MemoryFact]:
    return [_load(conn, row) for row in conn.execute("SELECT * FROM memory_facts WHERE fact_key=? ORDER BY fact_version", (fact_key,)).fetchall()]


def insert_fact(conn: sqlite3.Connection, fact: MemoryFact) -> None:
    conn.execute("""INSERT INTO memory_facts
        (fact_id,fact_key,fact_version,candidate_id,candidate_version,validation_id,decision_id,evaluation_id,
         target_type,subject_type,subject_id,predicate,value_json,scope_type,scope_id,evidence_refs_json,
         provenance_json,valid_from,valid_to,status) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,'active')""", (
        fact.fact_id, fact.fact_key, fact.version, fact.candidate_id, fact.candidate_version,
        fact.validation_id, fact.decision_id, fact.evaluation_id, fact.target_type, fact.subject_type,
        fact.subject_id, fact.predicate, json.dumps(fact.value, ensure_ascii=False, sort_keys=True),
        fact.scope_type, fact.scope_id, json.dumps(fact.evidence_refs, sort_keys=True),
        json.dumps(fact.provenance, ensure_ascii=False, sort_keys=True), fact.valid_from.isoformat(),
        fact.approved_valid_to.isoformat() if fact.approved_valid_to else None))


def append_state(conn: sqlite3.Connection, fact_id: str, *, state: Literal["active", "superseded", "revoked"], actor_id: str, reason: str, at: datetime, event_id: str) -> None:
    conn.execute("INSERT INTO memory_fact_state_events(event_id,fact_id,state,actor_id,reason,effective_at) VALUES (?,?,?,?,?,?)", (event_id, fact_id, state, actor_id, reason, at.isoformat()))
