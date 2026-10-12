"""Read helpers for exact governed candidate versions."""
from __future__ import annotations

import json
import sqlite3


def governed_candidate(conn: sqlite3.Connection, candidate_id: str, candidate_version: int) -> dict:
    row = conn.execute("""SELECT c.*, o.operator_id, s.disposition, s.validation_id, v.passed, v.risk_level,
        v.requires_evaluation FROM memory_candidates c
        JOIN learning_observations o ON o.observation_id=c.observation_id
        JOIN candidate_governance_states s USING(candidate_id,candidate_version)
        JOIN candidate_validation_runs v ON v.validation_id=s.validation_id
        WHERE c.candidate_id=? AND c.candidate_version=?""", (candidate_id, candidate_version)).fetchone()
    if row is None:
        raise KeyError((candidate_id, candidate_version))
    value = dict(row)
    value["proposed_value"] = json.loads(value["proposed_value_json"])
    value["evidence_refs"] = json.loads(value["evidence_refs_json"])
    return value
