"""Deterministic duplicate and typed contradiction analysis."""
from __future__ import annotations

import hashlib
import sqlite3
from typing import Literal

from pydantic import BaseModel, ConfigDict

from src.learning.governance.dedup import canonical_key, canonical_predicate, claims_equivalent, claims_opposed, semantic_fingerprint
from src.learning.governance.validator import load_candidate
from src.repositories.memory_conflict_repo import (
    CandidateRef, ConflictRelation, ConflictStatus, MatchType, RiskLevel, add_candidate_evidence,
    append_transition, create_or_extend_conflict, find_active_conflict_for_key, find_active_member_conflict, find_member_conflict, find_terminal_member_conflicts, get_conflict,
    get_semantic_matches, get_semantic_state, insert_semantic_key, is_authority_blocked,
)


class ConflictAnalysis(BaseModel):
    model_config = ConfigDict(frozen=True)
    candidate_id: str
    candidate_version: int
    match_type: Literal["unique", "distinct", "exact_duplicate", "near_duplicate", "contradiction"]
    canonical_candidate_id: str
    canonical_candidate_version: int
    conflict_id: str | None = None
    authority_blocked: bool = False


def analyse_candidate_conflicts(conn: sqlite3.Connection, candidate_id: str, candidate_version: int) -> ConflictAnalysis:
    ref = CandidateRef(candidate_id=candidate_id, candidate_version=candidate_version)
    persisted = get_semantic_state(conn, ref)
    if persisted is not None:
        return _persisted_result(conn, ref, persisted)
    candidate = load_candidate(conn, candidate_id, candidate_version)
    operator = conn.execute("SELECT operator_id FROM learning_observations WHERE observation_id=?", (candidate.observation_id,)).fetchone()
    if operator is None:
        raise ValueError("candidate source observation is missing")
    predicate = canonical_predicate(candidate.predicate)
    key = canonical_key(operator_id=operator[0], scope_type=candidate.scope_type, scope_id=candidate.scope_id, subject_type=candidate.subject_type, subject_id=candidate.subject_id, predicate=predicate)
    fingerprint = semantic_fingerprint(candidate.proposed_value, predicate=predicate)
    add_candidate_evidence(conn, ref, set(candidate.evidence_refs))
    matches = get_semantic_matches(conn, key)

    equivalent = None
    opposed = None
    for row in matches:
        existing_ref = CandidateRef(candidate_id=row.canonical_candidate_id, candidate_version=row.canonical_candidate_version)
        if find_terminal_member_conflicts(conn, existing_ref) and not find_active_member_conflict(conn, existing_ref):
            continue
        existing = load_candidate(conn, existing_ref.candidate_id, existing_ref.candidate_version)
        if claims_equivalent(predicate, existing.proposed_value, candidate.proposed_value):
            member_conflict_id = find_member_conflict(conn, existing_ref)
            if member_conflict_id is None or get_conflict(conn, member_conflict_id).status is ConflictStatus.UNRESOLVED:
                equivalent = (row, existing_ref)
                break
        if claims_opposed(predicate, existing.proposed_value, candidate.proposed_value):
            opposed = (row, existing_ref)

    if equivalent:
        row, canonical_ref = equivalent
        add_candidate_evidence(conn, canonical_ref, set(candidate.evidence_refs))
        match_type = "exact_duplicate" if row.value_fingerprint == fingerprint else "near_duplicate"
        prior_conflict_id = find_member_conflict(conn, canonical_ref)
        persisted_canonical = canonical_ref
        if prior_conflict_id and get_conflict(conn, prior_conflict_id).status is not ConflictStatus.UNRESOLVED:
            persisted_canonical = ref
        insert_semantic_key(conn, ref, canonical_key=key, value_fingerprint=fingerprint, canonical_ref=persisted_canonical, match_type=MatchType(match_type))
        conflict_id = find_active_conflict_for_key(conn, key)
        if conflict_id and find_member_conflict(conn, canonical_ref) == conflict_id:
            risk = _aggregate_risk(conn, get_conflict(conn, conflict_id).members + [ref])
            create_or_extend_conflict(conn, conflict_id=conflict_id, canonical_key=key, risk_level=risk, members=[canonical_ref, ref], relation=ConflictRelation(left=canonical_ref, right=ref, relation_type="equivalent"))
        else:
            conflict_id = None
        return _result(conn, ref, match_type, persisted_canonical, conflict_id)

    if opposed:
        _, opposite_ref = opposed
        insert_semantic_key(conn, ref, canonical_key=key, value_fingerprint=fingerprint, canonical_ref=ref, match_type=MatchType.CONTRADICTION)
        conflict_id = find_active_conflict_for_key(conn, key) or _conflict_id(key, opposite_ref, ref)
        active_members = get_conflict(conn, conflict_id).members if find_active_conflict_for_key(conn, key) else []
        risk = _aggregate_risk(conn, active_members + [opposite_ref, ref])
        create_or_extend_conflict(conn, conflict_id=conflict_id, canonical_key=key, risk_level=risk, members=[opposite_ref, ref], relation=ConflictRelation(left=opposite_ref, right=ref, relation_type="opposes"))
        return _result(conn, ref, "contradiction", ref, conflict_id)

    match_type = "unique" if not matches else "distinct"
    insert_semantic_key(conn, ref, canonical_key=key, value_fingerprint=fingerprint, canonical_ref=ref, match_type=MatchType(match_type))
    return _result(conn, ref, match_type, ref, None)


def transition_conflict(
    conn: sqlite3.Connection,
    conflict_id: str,
    *,
    status: ConflictStatus,
    actor_id: str,
    superseded_by_candidate_id: str | None = None,
    superseded_by_version: int | None = None,
    reason: str | None = None,
    action: str | None = None,
    confirmation_id: str | None = None,
) -> None:
    superseded = None
    if superseded_by_candidate_id is not None and superseded_by_version is not None:
        superseded = CandidateRef(candidate_id=superseded_by_candidate_id, candidate_version=superseded_by_version)
        if not conn.execute("SELECT 1 FROM memory_conflict_members WHERE conflict_id=? AND candidate_id=? AND candidate_version=?", (conflict_id, superseded.candidate_id, superseded.candidate_version)).fetchone():
            raise ValueError("superseding candidate must be a conflict member")
    append_transition(
        conn,
        conflict_id,
        status=status,
        actor_id=actor_id,
        superseded_by=superseded,
        reason=reason,
        action=action,
        confirmation_id=confirmation_id,
    )


def _persisted_result(conn, ref, state):
    canonical = CandidateRef(candidate_id=state.canonical_candidate_id, candidate_version=state.canonical_candidate_version)
    conflict_id = find_member_conflict(conn, ref)
    return _result(conn, ref, state.match_type.value, canonical, conflict_id)

def _result(conn, ref, match_type, canonical, conflict_id):
    return ConflictAnalysis(candidate_id=ref.candidate_id, candidate_version=ref.candidate_version, match_type=match_type, canonical_candidate_id=canonical.candidate_id, canonical_candidate_version=canonical.candidate_version, conflict_id=conflict_id, authority_blocked=is_authority_blocked(conn, ref.candidate_id, ref.candidate_version))


def _aggregate_risk(conn, refs):
    unique = {(ref.candidate_id, ref.candidate_version): ref for ref in refs}
    return max((_persisted_risk(conn, ref) for ref in unique.values()), key=_risk_rank, default=RiskLevel.LOW)


def _persisted_risk(conn, ref):
    row = conn.execute("""SELECT v.risk_level FROM candidate_governance_states s JOIN candidate_validation_runs v ON v.validation_id=s.validation_id WHERE s.candidate_id=? AND s.candidate_version=?""", (ref.candidate_id, ref.candidate_version)).fetchone()
    return RiskLevel(row[0]) if row else RiskLevel.LOW


def _conflict_id(key, left, right):
    identities = sorted([(left.candidate_id, left.candidate_version), (right.candidate_id, right.candidate_version)])
    return "mcf_" + hashlib.sha256(repr((key, identities)).encode("utf-8")).hexdigest()[:24]


def _risk_rank(value):
    return list(RiskLevel).index(value)
