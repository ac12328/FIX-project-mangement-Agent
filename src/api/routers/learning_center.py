"""Learning Center control-plane adapters over governed learning services."""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query

from src.api.auth import CurrentActor
from src.api.deps import get_current_actor, get_read_conn, get_write_actor
from src.api.schemas.learning_center import (
    ActionResponse, CandidateActionRequest, ConflictActionRequest, FactActionRequest,
    JobActionRequest, KillSwitchRequest, Page,
)
from src.events.bus import Event, event_bus

router = APIRouter(prefix="/learning-center", tags=["learning-center"])


def _loads(value: Any, fallback):
    if value is None:
        return fallback
    try:
        return json.loads(value)
    except (TypeError, json.JSONDecodeError):
        return fallback


def _page(rows, total: int, limit: int, offset: int) -> Page:
    return Page(items=[dict(row) for row in rows], total=total, limit=limit, offset=offset)


def _operator(actor: CurrentActor) -> str:
    return actor.operator_id


def _publish(event_type: str, detail: dict) -> None:
    event_bus.publish(Event(event_type=event_type, detail=detail, surface="dashboard"))


def _conflict(exc: Exception) -> HTTPException:
    if isinstance(exc, KeyError):
        return HTTPException(404, "Learning resource not found")
    if isinstance(exc, PermissionError):
        return HTTPException(403, str(exc))
    return HTTPException(409, str(exc))


def _confirmed(required: bool, supplied: bool) -> None:
    if required and not supplied:
        raise HTTPException(409, "Explicit confirmation is required for this high-risk action")


@router.get("/candidates", response_model=Page)
def list_candidates(
    status: str | None = None, risk_level: str | None = None,
    limit: int = Query(50, ge=1, le=200), offset: int = Query(0, ge=0),
    actor: CurrentActor = Depends(get_current_actor), conn: sqlite3.Connection = Depends(get_read_conn),
):
    clauses = ["o.operator_id=?"]
    params: list[Any] = [_operator(actor)]
    if status:
        clauses.append("g.disposition=?"); params.append(status)
    if risk_level:
        clauses.append("v.risk_level=?"); params.append(risk_level)
    where = " AND ".join(clauses)
    total = conn.execute(f"""SELECT COUNT(*) FROM memory_candidates c JOIN learning_observations o USING(observation_id)
      LEFT JOIN candidate_governance_states g USING(candidate_id,candidate_version)
      LEFT JOIN candidate_validation_runs v ON v.validation_id=g.validation_id WHERE {where}""", params).fetchone()[0]
    rows = conn.execute(f"""SELECT c.candidate_id,c.candidate_version,o.operator_id,c.target_type,c.subject_type,c.subject_id,
      c.predicate,c.proposed_value_json,c.scope_type,c.scope_id,c.confidence,c.suggested_expiry,c.reasoning_summary,
      c.evidence_refs_json,c.created_at,g.disposition,v.risk_level,v.passed,v.validation_id
      FROM memory_candidates c JOIN learning_observations o USING(observation_id)
      LEFT JOIN candidate_governance_states g USING(candidate_id,candidate_version)
      LEFT JOIN candidate_validation_runs v ON v.validation_id=g.validation_id WHERE {where}
      ORDER BY c.created_at DESC,c.candidate_id,c.candidate_version DESC LIMIT ? OFFSET ?""", [*params, limit, offset]).fetchall()
    items = []
    for row in rows:
        item = dict(row); item["proposed_value"] = _loads(item.pop("proposed_value_json"), None)
        item["evidence_refs"] = _loads(item.pop("evidence_refs_json"), []); items.append(item)
    return Page(items=items, total=total, limit=limit, offset=offset)


@router.get("/candidates/{candidate_id}/{candidate_version}")
def candidate_detail(candidate_id: str, candidate_version: int, actor: CurrentActor = Depends(get_current_actor),
                     conn: sqlite3.Connection = Depends(get_read_conn)):
    row = conn.execute("""SELECT c.*,o.* FROM memory_candidates c JOIN learning_observations o USING(observation_id)
      WHERE c.candidate_id=? AND c.candidate_version=? AND o.operator_id=?""",
      (candidate_id, candidate_version, _operator(actor))).fetchone()
    if not row: raise HTTPException(404, "Candidate not found")
    validation = conn.execute("""SELECT v.* FROM candidate_governance_states g JOIN candidate_validation_runs v
      ON v.validation_id=g.validation_id WHERE g.candidate_id=? AND g.candidate_version=?""", (candidate_id, candidate_version)).fetchone()
    evaluation = conn.execute("SELECT * FROM candidate_evaluations WHERE candidate_id=? AND candidate_version=? ORDER BY evaluated_at DESC LIMIT 1", (candidate_id, candidate_version)).fetchone()
    decision = conn.execute("SELECT * FROM memory_decisions WHERE candidate_id=? AND candidate_version=? ORDER BY decided_at DESC LIMIT 1", (candidate_id, candidate_version)).fetchone()
    fact = conn.execute("SELECT * FROM memory_facts WHERE candidate_id=? AND candidate_version=? ORDER BY fact_version DESC LIMIT 1", (candidate_id, candidate_version)).fetchone()
    exposures = []; outcomes = []; fact_versions = []; lifecycle = []; publications = []
    if fact:
        exposures = [dict(item) for item in conn.execute("SELECT * FROM memory_exposures WHERE fact_id=? AND fact_version=? ORDER BY created_at", (fact["fact_id"], fact["fact_version"])).fetchall()]
        outcomes = [dict(item) for item in conn.execute("SELECT * FROM learning_outcomes WHERE fact_id=? AND fact_version=? ORDER BY created_at", (fact["fact_id"], fact["fact_version"])).fetchall()]
        fact_versions = [dict(item) for item in conn.execute("SELECT * FROM memory_facts WHERE fact_key=? ORDER BY fact_version", (fact["fact_key"],)).fetchall()]
        lifecycle = [dict(item) for item in conn.execute("SELECT * FROM memory_lifecycle_events WHERE fact_id=? ORDER BY effective_at,event_id", (fact["fact_id"],)).fetchall()]
        publications = [dict(item) for item in conn.execute("SELECT * FROM wiki_publications WHERE fact_id=? ORDER BY created_at", (fact["fact_id"],)).fetchall()]
    evidence = [dict(item) for item in conn.execute("""SELECT o.* FROM candidate_evidence e JOIN learning_observations o USING(observation_id)
      WHERE e.candidate_id=? AND e.candidate_version=? AND o.operator_id=? ORDER BY o.occurred_at,o.observation_id""",
      (candidate_id, candidate_version, _operator(actor))).fetchall()]
    conflicts = [dict(item) for item in conn.execute("""SELECT c.* FROM memory_conflict_members m JOIN memory_conflicts c USING(conflict_id)
      WHERE m.candidate_id=? AND m.candidate_version=? ORDER BY c.created_at,c.conflict_id""", (candidate_id, candidate_version)).fetchall()]
    decisions = [dict(item) for item in conn.execute("SELECT * FROM memory_decisions WHERE candidate_id=? AND candidate_version=? ORDER BY decided_at,decision_id", (candidate_id, candidate_version)).fetchall()]
    candidate_keys = set(conn.execute("PRAGMA table_info(memory_candidates)").fetchall()[0].keys()) if False else None
    candidate = {key: row[key] for key in ("candidate_id","candidate_version","target_type","subject_type","subject_id","predicate","scope_type","scope_id","confidence","suggested_expiry","reasoning_summary","created_at")}
    candidate["proposed_value"] = _loads(row["proposed_value_json"], None); candidate["evidence_refs"] = _loads(row["evidence_refs_json"], [])
    observation = {key: row[key] for key in ("observation_id","source_type","source_id","project_id","contact_id","normalized_text","content_hash","sensitivity","trust_tier","occurred_at")}
    return {"candidate": candidate, "observation": observation, "evidence": evidence,
            "validation": dict(validation) if validation else None,
            "evaluation": dict(evaluation) if evaluation else None, "decision": dict(decision) if decision else None,
            "decisions": decisions, "fact": dict(fact) if fact else None, "fact_versions": fact_versions,
            "exposures": exposures, "outcomes": outcomes, "conflicts": conflicts,
            "publications": publications, "lifecycle": lifecycle}


@router.get("/facts", response_model=Page)
def list_facts(limit: int = Query(50, ge=1, le=200), offset: int = Query(0, ge=0),
               actor: CurrentActor = Depends(get_current_actor), conn: sqlite3.Connection = Depends(get_read_conn)):
    params = (_operator(actor),)
    total = conn.execute("""SELECT COUNT(*) FROM memory_facts f JOIN memory_candidates c USING(candidate_id,candidate_version)
      JOIN learning_observations o USING(observation_id) WHERE o.operator_id=?""", params).fetchone()[0]
    rows = conn.execute("""SELECT f.* FROM memory_facts f JOIN memory_candidates c USING(candidate_id,candidate_version)
      JOIN learning_observations o USING(observation_id) WHERE o.operator_id=? ORDER BY f.valid_from DESC LIMIT ? OFFSET ?""",
      (*params, limit, offset)).fetchall()
    return _page(rows, total, limit, offset)


@router.get("/facts/{fact_id}")
def fact_detail(fact_id: str, actor: CurrentActor = Depends(get_current_actor),
                conn: sqlite3.Connection = Depends(get_read_conn)):
    row = conn.execute("""SELECT f.* FROM memory_facts f JOIN memory_candidates c USING(candidate_id,candidate_version)
      JOIN learning_observations o USING(observation_id) WHERE f.fact_id=? AND o.operator_id=?""",
      (fact_id, _operator(actor))).fetchone()
    if not row: raise HTTPException(404, "Fact not found")
    fact = dict(row)
    versions = [dict(item) for item in conn.execute("SELECT * FROM memory_facts WHERE fact_key=? ORDER BY fact_version", (row["fact_key"],)).fetchall()]
    decisions = [dict(item) for item in conn.execute("SELECT * FROM memory_decisions WHERE candidate_id=? ORDER BY decided_at,decision_id", (row["candidate_id"],)).fetchall()]
    exposures = [dict(item) for item in conn.execute("SELECT * FROM memory_exposures WHERE fact_id=? ORDER BY created_at", (fact_id,)).fetchall()]
    outcomes = [dict(item) for item in conn.execute("SELECT * FROM learning_outcomes WHERE fact_id=? ORDER BY created_at", (fact_id,)).fetchall()]
    lifecycle = [dict(item) for item in conn.execute("SELECT * FROM memory_lifecycle_events WHERE fact_id=? ORDER BY effective_at,event_id", (fact_id,)).fetchall()]
    states = [dict(item) for item in conn.execute("SELECT * FROM memory_fact_state_events WHERE fact_id=? ORDER BY effective_at,created_at,event_seq", (fact_id,)).fetchall()]
    rollbacks = [dict(item) for item in conn.execute("""SELECT r.*,op.status AS operation_status,op.reason AS operation_reason
      FROM memory_rollback_targets r JOIN memory_lifecycle_operations op USING(operation_id)
      WHERE r.fact_id=? ORDER BY op.effective_at,r.operation_id,r.target""", (fact_id,)).fetchall()]
    publications = [dict(item) for item in conn.execute("SELECT * FROM wiki_publications WHERE fact_id=? ORDER BY created_at", (fact_id,)).fetchall()]
    return {"fact": fact, "versions": versions, "decisions": decisions, "state_timeline": states,
            "lifecycle": lifecycle, "exposures": exposures, "outcomes": outcomes,
            "rollbacks": rollbacks, "publications": publications}


@router.get("/conflicts", response_model=Page)
def list_conflicts(limit: int = Query(50, ge=1, le=200), offset: int = Query(0, ge=0),
                   actor: CurrentActor = Depends(get_current_actor), conn: sqlite3.Connection = Depends(get_read_conn)):
    total = conn.execute("""SELECT COUNT(DISTINCT c.conflict_id) FROM memory_conflicts c JOIN memory_conflict_members m USING(conflict_id)
      JOIN memory_candidates mc USING(candidate_id,candidate_version) JOIN learning_observations o USING(observation_id) WHERE o.operator_id=?""", (_operator(actor),)).fetchone()[0]
    rows = conn.execute("""SELECT DISTINCT c.* FROM memory_conflicts c JOIN memory_conflict_members m USING(conflict_id)
      JOIN memory_candidates mc USING(candidate_id,candidate_version) JOIN learning_observations o USING(observation_id)
      WHERE o.operator_id=? ORDER BY c.updated_at DESC LIMIT ? OFFSET ?""", (_operator(actor), limit, offset)).fetchall()
    return _page(rows, total, limit, offset)


@router.get("/jobs", response_model=Page)
def list_jobs(limit: int = Query(50, ge=1, le=200), offset: int = Query(0, ge=0),
              actor: CurrentActor = Depends(get_current_actor), conn: sqlite3.Connection = Depends(get_read_conn)):
    total = conn.execute("SELECT COUNT(*) FROM learning_outbox j JOIN learning_observations o USING(observation_id) WHERE o.operator_id=?", (_operator(actor),)).fetchone()[0]
    rows = conn.execute("""SELECT j.* FROM learning_outbox j JOIN learning_observations o USING(observation_id)
      WHERE o.operator_id=? ORDER BY j.updated_at DESC LIMIT ? OFFSET ?""", (_operator(actor), limit, offset)).fetchall()
    return _page(rows, total, limit, offset)


@router.get("/rollbacks", response_model=Page)
def list_rollbacks(limit: int = Query(50, ge=1, le=200), offset: int = Query(0, ge=0),
                   actor: CurrentActor = Depends(get_current_actor), conn: sqlite3.Connection = Depends(get_read_conn)):
    params = (_operator(actor),)
    base = """FROM memory_lifecycle_operations op WHERE EXISTS (
      SELECT 1 FROM memory_rollback_targets r JOIN memory_facts f USING(fact_id)
      JOIN memory_candidates c USING(candidate_id,candidate_version)
      JOIN learning_observations o USING(observation_id)
      WHERE r.operation_id=op.operation_id AND o.operator_id=?)"""
    total = conn.execute("SELECT COUNT(*) " + base, params).fetchone()[0]
    rows = conn.execute("SELECT op.* " + base + " ORDER BY op.effective_at DESC LIMIT ? OFFSET ?",
                        (*params, limit, offset)).fetchall()
    return _page(rows, total, limit, offset)


@router.get("/audit", response_model=Page)
def list_audit(limit: int = Query(100, ge=1, le=300), offset: int = Query(0, ge=0),
               actor: CurrentActor = Depends(get_current_actor), conn: sqlite3.Connection = Depends(get_read_conn)):
    total = conn.execute("""SELECT COUNT(*) FROM activity_log WHERE actor=? AND surface='learning_governance'""", (_operator(actor),)).fetchone()[0]
    rows = conn.execute("""SELECT id,actor,surface,action_type,object_ref,summary,metadata_json,created_at FROM activity_log
      WHERE actor=? AND surface='learning_governance' ORDER BY created_at DESC,id DESC LIMIT ? OFFSET ?""", (_operator(actor), limit, offset)).fetchall()
    return _page(rows, total, limit, offset)


@router.get("/kill-switches")
def get_kill_switches(actor: CurrentActor = Depends(get_current_actor)):
    from src.learning.governance_flags import operational_state
    return {"items": [{"name": name, "effective": value} for name, value in operational_state().items()]}
