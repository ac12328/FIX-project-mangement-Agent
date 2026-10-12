"""Immutable operator-authored candidate revision service."""
from __future__ import annotations

import json
import sqlite3

from src.learning.governance.contracts import CandidateRevision
from src.learning.governance.validator import load_candidate


def append_operator_revision(conn: sqlite3.Connection, candidate_id: str, candidate_version: int,
                             revision: CandidateRevision, *, source_validation_id: str) -> object:
    """Append a new proposal version without weakening later validation/approval gates."""
    current = load_candidate(conn, candidate_id, candidate_version)
    state = conn.execute(
        "SELECT validation_id FROM candidate_governance_states WHERE candidate_id=? AND candidate_version=?",
        (candidate_id, candidate_version),
    ).fetchone()
    if not state or state[0] != source_validation_id:
        raise ValueError("candidate state changed before revision")
    next_version = conn.execute(
        "SELECT COALESCE(MAX(candidate_version),0)+1 FROM memory_candidates WHERE candidate_id=?",
        (candidate_id,),
    ).fetchone()[0]
    values = revision.model_dump(exclude_none=True, mode="json")
    conn.execute(
        """INSERT INTO memory_candidates
           (candidate_id,observation_id,candidate_version,target_type,subject_type,subject_id,predicate,
            proposed_value_json,scope_type,scope_id,confidence,suggested_expiry,reasoning_summary,
            risk_signals_json,evidence_refs_json,status,extractor_version,model_version,prompt_version,
            schema_version,revision_of_validation_id)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,'proposed',?,?,?,?,?)""",
        (candidate_id, current.observation_id, next_version, current.target_type,
         values.get("subject_type", current.subject_type), values.get("subject_id", current.subject_id),
         values.get("predicate", current.predicate), json.dumps(revision.proposed_value, ensure_ascii=False, sort_keys=True),
         values.get("scope_type", current.scope_type), values.get("scope_id", current.scope_id), current.confidence,
         values.get("suggested_expiry", current.suggested_expiry), revision.reasoning_summary,
         json.dumps(values.get("risk_signals", current.risk_signals), ensure_ascii=False, sort_keys=True),
         json.dumps(current.evidence_refs, ensure_ascii=False, sort_keys=True), current.extractor_version,
         current.model_version, current.prompt_version, current.schema_version, source_validation_id),
    )
    return load_candidate(conn, candidate_id, next_version)
