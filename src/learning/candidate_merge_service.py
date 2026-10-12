"""Immutable governed persistence for candidate equivalence merges."""
from __future__ import annotations

import hashlib
import sqlite3
from datetime import datetime, timezone

from pydantic import BaseModel, ConfigDict

from src.learning.governance.contradiction import analyse_candidate_conflicts
from src.repositories.memory_conflict_repo import CandidateRef, add_candidate_evidence, get_candidate_evidence


class CandidateMergeResult(BaseModel):
    model_config = ConfigDict(frozen=True)
    status: str
    decision_id: str | None = None
    conflict_id: str | None = None


def merge_candidates(
    conn: sqlite3.Connection,
    *,
    canonical_id: str,
    canonical_version: int,
    merged_id: str,
    merged_version: int,
    actor_id: str,
    reason: str,
    idempotency_key: str,
) -> CandidateMergeResult:
    decision_id = "cmd_" + hashlib.sha256(idempotency_key.encode("utf-8")).hexdigest()[:24]
    existing = conn.execute(
        "SELECT conflict_id FROM candidate_merge_decisions WHERE decision_id=?",
        (decision_id,),
    ).fetchone()
    if existing:
        return CandidateMergeResult(status="merged", decision_id=decision_id, conflict_id=existing[0])

    left = analyse_candidate_conflicts(conn, canonical_id, canonical_version)
    right = analyse_candidate_conflicts(conn, merged_id, merged_version)
    equivalent = (
        right.match_type in {"exact_duplicate", "near_duplicate"}
        and (right.canonical_candidate_id, right.canonical_candidate_version) == (canonical_id, canonical_version)
    ) or (
        left.match_type in {"exact_duplicate", "near_duplicate"}
        and (left.canonical_candidate_id, left.canonical_candidate_version) == (merged_id, merged_version)
    )
    if not equivalent:
        return CandidateMergeResult(status="not_merged")

    conflict_id = right.conflict_id or left.conflict_id
    evidence = get_candidate_evidence(conn, merged_id, merged_version)
    add_candidate_evidence(
        conn,
        CandidateRef(candidate_id=canonical_id, candidate_version=canonical_version),
        evidence,
    )
    conn.execute(
        """INSERT INTO candidate_merge_decisions(
               decision_id,idempotency_key,canonical_candidate_id,canonical_candidate_version,
               merged_candidate_id,merged_candidate_version,conflict_id,actor_id,reason,created_at)
           VALUES (?,?,?,?,?,?,?,?,?,?)""",
        (
            decision_id,
            idempotency_key,
            canonical_id,
            canonical_version,
            merged_id,
            merged_version,
            conflict_id,
            actor_id,
            reason,
            datetime.now(timezone.utc).isoformat(),
        ),
    )
    return CandidateMergeResult(status="merged", decision_id=decision_id, conflict_id=conflict_id)
