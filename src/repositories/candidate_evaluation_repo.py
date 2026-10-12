"""Typed append-only persistence for candidate evaluation records."""
from __future__ import annotations

import sqlite3
from typing import Any

from pydantic import BaseModel, ConfigDict


class EvaluationRecord(BaseModel):
    """Immutable database representation of one evaluation run."""

    model_config = ConfigDict(frozen=True)
    evaluation_id: str
    candidate_id: str
    candidate_version: int
    runner_version: str
    policy_version: str
    suite_manifest_json: str
    baseline_outputs_json: str
    candidate_outputs_json: str
    scores_json: str
    safety_assertions_json: str
    failure_codes_json: str
    suite_hash: str
    config_hash: str
    replay_hash: str
    runtime_hash: str
    input_hash: str
    output_hash: str
    passed: bool
    evaluated_at: str
    created_at: str


def insert_evaluation(conn: sqlite3.Connection, record: EvaluationRecord) -> None:
    """Insert one append-only typed evaluation record."""
    values = record.model_dump()
    columns = list(values)
    placeholders = ",".join("?" for _ in columns)
    parameters = [int(value) if isinstance(value, bool) else value for value in values.values()]
    conn.execute(
        f"INSERT INTO candidate_evaluations({','.join(columns)}) VALUES ({placeholders})",
        parameters,
    )


def latest_evaluation(conn: sqlite3.Connection, candidate_id: str,
                      candidate_version: int) -> EvaluationRecord | None:
    """Load the newest immutable evaluation for a candidate version."""
    row = conn.execute(
        "SELECT * FROM candidate_evaluations WHERE candidate_id=? AND candidate_version=? "
        "ORDER BY evaluated_at DESC, created_at DESC, evaluation_id DESC LIMIT 1",
        (candidate_id, candidate_version),
    ).fetchone()
    return EvaluationRecord(**dict(row)) if row else None


def candidate_snapshot(conn: sqlite3.Connection, candidate_id: str,
                       candidate_version: int) -> dict[str, Any]:
    """Read the current candidate, governance, validation, and conflict state."""
    candidate = conn.execute(
        "SELECT * FROM memory_candidates WHERE candidate_id=? AND candidate_version=?",
        (candidate_id, candidate_version),
    ).fetchone()
    if candidate is None:
        raise KeyError((candidate_id, candidate_version))
    state = conn.execute(
        "SELECT s.disposition, v.risk_level, v.validation_id "
        "FROM candidate_governance_states s "
        "JOIN candidate_validation_runs v ON v.validation_id=s.validation_id "
        "WHERE s.candidate_id=? AND s.candidate_version=?",
        (candidate_id, candidate_version),
    ).fetchone()
    blocked = conn.execute(
        "SELECT 1 FROM memory_conflict_members m JOIN memory_conflicts c USING(conflict_id) "
        "WHERE m.candidate_id=? AND m.candidate_version=? AND c.status='unresolved' "
        "AND c.authority_blocked=1 LIMIT 1",
        (candidate_id, candidate_version),
    ).fetchone()
    return {
        "candidate": dict(candidate),
        "disposition": state[0] if state else None,
        "risk_level": state[1] if state else "critical",
        "validation_id": state[2] if state else None,
        "authority_blocked": bool(blocked),
    }
