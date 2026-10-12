"""Typed persistence for candidate semantic keys and append-only conflict history."""
from __future__ import annotations

import sqlite3
from enum import Enum
from typing import Literal

from pydantic import BaseModel, ConfigDict


class GovernanceIntegrityError(RuntimeError):
    """Raised when persisted conflict membership violates governance invariants."""


class RiskLevel(str, Enum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    CRITICAL = "critical"


class MatchType(str, Enum):
    UNIQUE = "unique"
    DISTINCT = "distinct"
    EXACT_DUPLICATE = "exact_duplicate"
    NEAR_DUPLICATE = "near_duplicate"
    CONTRADICTION = "contradiction"


class ConflictStatus(str, Enum):
    UNRESOLVED = "unresolved"
    AUTO_RESOLVED = "auto-resolved"
    REVIEW_RESOLVED = "review-resolved"
    SUPERSEDED = "superseded"


class CandidateRef(BaseModel):
    model_config = ConfigDict(frozen=True)
    candidate_id: str
    candidate_version: int


class SemanticState(BaseModel):
    model_config = ConfigDict(frozen=True)
    candidate_id: str
    candidate_version: int
    canonical_key: str
    value_fingerprint: str
    canonical_candidate_id: str
    canonical_candidate_version: int
    match_type: MatchType

    @classmethod
    def ref(cls, candidate_id: str, candidate_version: int) -> CandidateRef:
        return CandidateRef(candidate_id=candidate_id, candidate_version=candidate_version)


class ConflictRelation(BaseModel):
    model_config = ConfigDict(frozen=True)
    left: CandidateRef
    right: CandidateRef
    relation_type: Literal["equivalent", "opposes", "supersedes"]


class MemoryConflict(BaseModel):
    model_config = ConfigDict(frozen=True)
    conflict_id: str
    canonical_key: str
    status: ConflictStatus
    risk_level: RiskLevel
    authority_blocked: bool
    superseded_by_candidate_id: str | None
    superseded_by_version: int | None
    members: list[CandidateRef]
    relations: list[ConflictRelation]


def add_candidate_evidence(conn: sqlite3.Connection, ref: CandidateRef, observation_ids: set[str]) -> None:
    for observation_id in sorted(observation_ids):
        conn.execute("INSERT OR IGNORE INTO candidate_evidence(candidate_id, candidate_version, observation_id) VALUES (?, ?, ?)", (ref.candidate_id, ref.candidate_version, observation_id))


def get_candidate_evidence(conn: sqlite3.Connection, candidate_id: str, candidate_version: int) -> set[str]:
    return {row[0] for row in conn.execute("SELECT observation_id FROM candidate_evidence WHERE candidate_id=? AND candidate_version=?", (candidate_id, candidate_version)).fetchall()}


def _state(row) -> SemanticState:
    return SemanticState(**dict(row))


def get_semantic_matches(conn: sqlite3.Connection, canonical_key: str) -> list[SemanticState]:
    return [_state(row) for row in conn.execute("SELECT * FROM candidate_semantic_keys WHERE canonical_key=? ORDER BY candidate_id, candidate_version", (canonical_key,)).fetchall()]


def get_semantic_state(conn: sqlite3.Connection, ref: CandidateRef) -> SemanticState | None:
    row = conn.execute("SELECT * FROM candidate_semantic_keys WHERE candidate_id=? AND candidate_version=?", (ref.candidate_id, ref.candidate_version)).fetchone()
    return _state(row) if row else None


def insert_semantic_key(conn: sqlite3.Connection, ref: CandidateRef, *, canonical_key: str, value_fingerprint: str, canonical_ref: CandidateRef, match_type: MatchType) -> None:
    conn.execute("""INSERT OR IGNORE INTO candidate_semantic_keys(candidate_id, candidate_version, canonical_key, value_fingerprint, canonical_candidate_id, canonical_candidate_version, match_type) VALUES (?, ?, ?, ?, ?, ?, ?)""", (ref.candidate_id, ref.candidate_version, canonical_key, value_fingerprint, canonical_ref.candidate_id, canonical_ref.candidate_version, match_type.value))


def find_active_conflict_for_key(conn: sqlite3.Connection, canonical_key: str) -> str | None:
    row = conn.execute("SELECT conflict_id FROM memory_conflicts WHERE canonical_key=? AND status='unresolved' ORDER BY created_at DESC, conflict_id DESC LIMIT 1", (canonical_key,)).fetchone()
    return row[0] if row else None


def find_active_member_conflict(conn: sqlite3.Connection, ref: CandidateRef) -> str | None:
    rows = conn.execute("""SELECT m.conflict_id FROM memory_conflict_members m JOIN memory_conflicts c USING(conflict_id) WHERE m.candidate_id=? AND m.candidate_version=? AND c.status='unresolved' ORDER BY c.created_at, m.conflict_id""", (ref.candidate_id, ref.candidate_version)).fetchall()
    if len(rows) > 1:
        raise GovernanceIntegrityError("candidate belongs to multiple unresolved conflicts")
    return rows[0][0] if rows else None


def find_terminal_member_conflicts(conn: sqlite3.Connection, ref: CandidateRef) -> list[str]:
    return [row[0] for row in conn.execute("""SELECT m.conflict_id FROM memory_conflict_members m JOIN memory_conflicts c USING(conflict_id) WHERE m.candidate_id=? AND m.candidate_version=? AND c.status!='unresolved' ORDER BY c.created_at, m.conflict_id""", (ref.candidate_id, ref.candidate_version)).fetchall()]


def find_member_conflict(conn: sqlite3.Connection, ref: CandidateRef) -> str | None:
    return find_active_member_conflict(conn, ref) or next(iter(find_terminal_member_conflicts(conn, ref)), None)


def create_or_extend_conflict(conn: sqlite3.Connection, *, conflict_id: str, canonical_key: str, risk_level: RiskLevel, members: list[CandidateRef], relation: ConflictRelation) -> None:
    blocked = risk_level in {RiskLevel.HIGH, RiskLevel.CRITICAL}
    existing = conn.execute("SELECT risk_level, status FROM memory_conflicts WHERE conflict_id=?", (conflict_id,)).fetchone()
    if existing is not None and existing[1] != ConflictStatus.UNRESOLVED.value:
        raise ValueError("conflict extension requires unresolved status")
    for ref in members:
        active = find_active_member_conflict(conn, ref)
        if active is not None and active != conflict_id:
            raise GovernanceIntegrityError("candidate already belongs to a different unresolved conflict")
    if existing is None:
        conn.execute("INSERT INTO memory_conflicts(conflict_id, canonical_key, status, risk_level, authority_blocked) VALUES (?, ?, 'unresolved', ?, ?)", (conflict_id, canonical_key, risk_level.value, int(blocked)))
        _append_event(conn, conflict_id, ConflictStatus.UNRESOLVED, actor_id="system", risk_level=risk_level)
    elif _risk_rank(risk_level) > _risk_rank(RiskLevel(existing[0])):
        conn.execute("UPDATE memory_conflicts SET risk_level=?, authority_blocked=? WHERE conflict_id=?", (risk_level.value, int(blocked), conflict_id))
        _append_event(conn, conflict_id, ConflictStatus.UNRESOLVED, actor_id="system", risk_level=risk_level, event_type="risk-escalated")
    for ref in members:
        conn.execute("INSERT OR IGNORE INTO memory_conflict_members(conflict_id, candidate_id, candidate_version) VALUES (?, ?, ?)", (conflict_id, ref.candidate_id, ref.candidate_version))
    conn.execute("""INSERT OR IGNORE INTO memory_conflict_relations(conflict_id, left_candidate_id, left_candidate_version, right_candidate_id, right_candidate_version, relation_type) VALUES (?, ?, ?, ?, ?, ?)""", (conflict_id, relation.left.candidate_id, relation.left.candidate_version, relation.right.candidate_id, relation.right.candidate_version, relation.relation_type))


def get_conflict(conn: sqlite3.Connection, conflict_id: str) -> MemoryConflict:
    row = conn.execute("SELECT * FROM memory_conflicts WHERE conflict_id=?", (conflict_id,)).fetchone()
    if row is None:
        raise KeyError(conflict_id)
    members = [CandidateRef(candidate_id=item[0], candidate_version=item[1]) for item in conn.execute("SELECT candidate_id, candidate_version FROM memory_conflict_members WHERE conflict_id=? ORDER BY candidate_id, candidate_version", (conflict_id,)).fetchall()]
    relations = [ConflictRelation(left=CandidateRef(candidate_id=item[0], candidate_version=item[1]), right=CandidateRef(candidate_id=item[2], candidate_version=item[3]), relation_type=item[4]) for item in conn.execute("SELECT left_candidate_id, left_candidate_version, right_candidate_id, right_candidate_version, relation_type FROM memory_conflict_relations WHERE conflict_id=? ORDER BY relation_id", (conflict_id,)).fetchall()]
    return MemoryConflict(conflict_id=row["conflict_id"], canonical_key=row["canonical_key"], status=ConflictStatus(row["status"]), risk_level=RiskLevel(row["risk_level"]), authority_blocked=bool(row["authority_blocked"]), superseded_by_candidate_id=row["superseded_by_candidate_id"], superseded_by_version=row["superseded_by_version"], members=members, relations=relations)


def is_authority_blocked(conn: sqlite3.Connection, candidate_id: str, candidate_version: int) -> bool:
    return conn.execute("""SELECT 1 FROM memory_conflict_members m JOIN memory_conflicts c USING(conflict_id) WHERE m.candidate_id=? AND m.candidate_version=? AND c.status='unresolved' AND c.authority_blocked=1 LIMIT 1""", (candidate_id, candidate_version)).fetchone() is not None


def append_transition(
    conn: sqlite3.Connection,
    conflict_id: str,
    *,
    status: ConflictStatus,
    actor_id: str,
    superseded_by: CandidateRef | None = None,
    reason: str | None = None,
    action: str | None = None,
    confirmation_id: str | None = None,
) -> None:
    current = get_conflict(conn, conflict_id)
    if current.status is status:
        same_id = current.superseded_by_candidate_id == (superseded_by.candidate_id if superseded_by else None)
        same_version = current.superseded_by_version == (superseded_by.candidate_version if superseded_by else None)
        if same_id and same_version:
            return
        raise ValueError("terminal transition target does not match persisted state")
    allowed = {ConflictStatus.UNRESOLVED: {ConflictStatus.AUTO_RESOLVED, ConflictStatus.REVIEW_RESOLVED, ConflictStatus.SUPERSEDED}}
    if status not in allowed.get(current.status, set()):
        raise ValueError(f"invalid conflict transition {current.status.value} -> {status.value}")
    if status is ConflictStatus.SUPERSEDED and superseded_by is None:
        raise ValueError("superseded transition requires candidate")
    _append_event(
        conn,
        conflict_id,
        status,
        actor_id=actor_id,
        risk_level=current.risk_level,
        superseded_by=superseded_by,
        reason=reason,
        action=action,
        confirmation_id=confirmation_id,
    )
    conn.execute("UPDATE memory_conflicts SET status=?, superseded_by_candidate_id=?, superseded_by_version=?, updated_at=datetime('now') WHERE conflict_id=?", (status.value, superseded_by.candidate_id if superseded_by else None, superseded_by.candidate_version if superseded_by else None, conflict_id))


def _append_event(
    conn,
    conflict_id,
    status,
    *,
    actor_id,
    risk_level,
    superseded_by=None,
    event_type="status-changed",
    reason=None,
    action=None,
    confirmation_id=None,
):
    seq = conn.execute("SELECT COALESCE(MAX(event_seq), 0) + 1 FROM memory_conflict_events WHERE conflict_id=?", (conflict_id,)).fetchone()[0]
    conn.execute(
        """INSERT INTO memory_conflict_events(
               conflict_id,event_seq,status,risk_level,actor_id,
               superseded_by_candidate_id,superseded_by_version,event_type,
               reason,action,confirmation_id)
           VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
        (
            conflict_id,
            seq,
            status.value,
            risk_level.value,
            actor_id,
            superseded_by.candidate_id if superseded_by else None,
            superseded_by.candidate_version if superseded_by else None,
            event_type,
            reason,
            action,
            confirmation_id,
        ),
    )


def _risk_rank(value: RiskLevel) -> int:
    return list(RiskLevel).index(value)
