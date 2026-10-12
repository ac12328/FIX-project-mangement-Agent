"""Authorization-first retrieval from canonical immutable memory facts."""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from config import defaults
from src.db.connection import scoped_read
from src.memory.ranking import RankingContext, rank_facts

Surface = Literal["chat", "web", "draft", "advisory", "tool"]
Profile = Literal["bootstrap", "minimal", "full"]
_SCOPE_FIELDS = {
    "contact": "contact_id",
    "project": "project_id",
    "counterparty": "counterparty_id",
    "operator": "operator_id",
    "market": "market",
}


class MemoryRetrievalRequest(BaseModel):
    model_config = ConfigDict(frozen=True)
    operator_id: str | None
    project_id: str | None = None
    contact_id: str | None = None
    counterparty_id: str | None = None
    market: str | None = None
    organization_id: str | None = None
    task_type: str | None = None
    surface: Surface
    profile: Profile = "full"
    query: str = ""
    at: datetime

    @field_validator("at")
    @classmethod
    def _aware_utc(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None or value.utcoffset().total_seconds() != 0:
            raise ValueError("at must be aware UTC")
        return value


class RetrievedMemoryFact(BaseModel):
    model_config = ConfigDict(frozen=True)
    fact_id: str
    fact_key: str
    version: int
    operator_id: str
    candidate_id: str
    candidate_version: int
    target_type: str
    subject_type: str
    subject_id: str | None = None
    predicate: str
    value: Any
    scope_type: str
    scope_id: str | None = None
    source_type: str
    trust_tier: str
    confidence: float = Field(ge=0.0, le=1.0)
    evidence_count: int = Field(ge=0)
    valid_from: datetime
    approved_valid_to: datetime | None = None
    attributed_outcome_score: float = 0.0
    conflict_penalty: float = Field(default=0.0, ge=0.0)

    @field_validator("valid_from", "approved_valid_to")
    @classmethod
    def _fact_times_aware_utc(cls, value: datetime | None) -> datetime | None:
        if value is None:
            return None
        if value.tzinfo is None or value.utcoffset() is None or value.utcoffset().total_seconds() != 0:
            raise ValueError("memory fact timestamps must be aware UTC")
        return value


class MemoryConflictWarning(BaseModel):
    model_config = ConfigDict(frozen=True)
    conflict_id: str
    fact_id: str
    canonical_key: str
    risk_level: str
    message: str = "Unresolved memory conflict blocks authoritative use."


class MemoryRetrievalResult(BaseModel):
    model_config = ConfigDict(frozen=True)
    facts: list[RetrievedMemoryFact] = Field(default_factory=list)
    warnings: list[MemoryConflictWarning] = Field(default_factory=list)
    diagnostics: list[str] = Field(default_factory=list)


class MemoryRetriever:
    """Retrieve authorized facts; ranking never sees unauthorized candidates."""

    def __init__(self, connection_factory=scoped_read):
        self._connection_factory = connection_factory

    def retrieve(self, request: MemoryRetrievalRequest) -> MemoryRetrievalResult:
        if not request.operator_id:
            return MemoryRetrievalResult(diagnostics=["missing_operator_authorization"])
        with self._connection_factory() as conn:
            facts, warnings, diagnostics = self._authorized_candidates(conn, request)
        ranked = rank_facts(facts, RankingContext(query=request.query, at=request.at))
        return MemoryRetrievalResult(facts=ranked, warnings=warnings, diagnostics=diagnostics)

    def authorized_versions(self, conn: sqlite3.Connection, request: MemoryRetrievalRequest) -> set[tuple[str, int]]:
        """Share canonical eligibility with compatibility prompt filters before ranking."""
        if not request.operator_id:
            return set()
        facts, _, _ = self._authorized_candidates(conn, request)
        return {(fact.fact_id, fact.version) for fact in facts}

    def _authorized_candidates(self, conn: sqlite3.Connection, request: MemoryRetrievalRequest):
        rows = conn.execute(
            """SELECT h.fact_key AS head_key,h.fact_id AS head_id,h.fact_version AS head_version,
                      f.*,c.confidence,c.created_at AS candidate_created_at,
                      o.source_type,o.trust_tier
                 FROM memory_facts f
                 LEFT JOIN memory_fact_heads h ON h.fact_key=f.fact_key
                 JOIN memory_candidates c ON c.candidate_id=f.candidate_id AND c.candidate_version=f.candidate_version
                 JOIN learning_observations o ON o.observation_id=c.observation_id
                ORDER BY f.fact_key,f.fact_version,f.fact_id"""
        ).fetchall()
        active_rows: dict[str, sqlite3.Row] = {}
        diagnostics: list[str] = []
        rows_by_key: dict[str, list[sqlite3.Row]] = {}
        for row in rows:
            rows_by_key.setdefault(row["fact_key"], []).append(row)
        valid_keys = {
            key for key, key_rows in rows_by_key.items()
            if self._canonical_head_valid(conn, key_rows)
        }
        if len(valid_keys) != len(rows_by_key):
            diagnostics.append("canonical_integrity_failure")
        for row in rows:
            if row["fact_key"] not in valid_keys:
                continue
            if not self._state_active_at(conn, row, request.at):
                continue
            previous = active_rows.get(row["fact_key"])
            if previous is None or (row["fact_version"], row["fact_id"]) > (previous["fact_version"], previous["fact_id"]):
                active_rows[row["fact_key"]] = row
        facts: list[RetrievedMemoryFact] = []
        warnings: list[MemoryConflictWarning] = []
        for row in active_rows.values():
            if row["scope_type"] == "global" and defaults.GLOBAL_MEMORY_DISABLED:
                diagnostics.append("global_memory_disabled")
                continue
            valid_from = _parse_time(row["valid_from"])
            if valid_from is None or valid_from > request.at:
                continue
            observation = conn.execute(
                """SELECT o.source_id,o.content_hash,o.raw_payload_json FROM memory_candidates c
                   JOIN learning_observations o ON o.observation_id=c.observation_id
                   WHERE c.candidate_id=? AND c.candidate_version=?""",
                (row["candidate_id"], row["candidate_version"]),
            ).fetchone()
            from src.learning.authority import authority_denied
            if observation is None or authority_denied(conn, observation):
                diagnostics.append("authority_denylist_blocked")
                continue
            if defaults.MEMORY_LIFECYCLE_V2_ENABLED:
                from src.learning.lifecycle_policy import bound_policy_valid
                if not bound_policy_valid(conn, row["fact_id"]):
                    diagnostics.append("lifecycle_policy_integrity_failure")
                    continue
            provenance = _json(row["provenance_json"], {})
            operator_id = str(provenance.get("operator_id") or "")
            if operator_id != request.operator_id or not _scope_authorized(row, request):
                continue
            governed_validity = conn.execute(
                """SELECT valid_to FROM memory_fact_validity_events WHERE fact_id=? AND effective_at<=?
                   ORDER BY effective_at DESC,created_at DESC,event_id DESC LIMIT 1""",
                (row["fact_id"], request.at.isoformat()),
            ).fetchone()
            policy_expiry = conn.execute(
                "SELECT approved_valid_to FROM memory_fact_lifecycle_policy WHERE fact_id=?", (row["fact_id"],),
            ).fetchone()
            valid_to = _parse_time(governed_validity[0] if governed_validity else (policy_expiry[0] if policy_expiry else row["valid_to"]))
            if valid_to is None or valid_to <= request.at:
                diagnostics.append("missing_or_expired_lifecycle_validity")
                continue
            conflict = conn.execute(
                """SELECT c.conflict_id,c.canonical_key,c.risk_level,c.authority_blocked
                     FROM memory_conflict_members m JOIN memory_conflicts c USING(conflict_id)
                    WHERE m.candidate_id=? AND m.candidate_version=? AND c.status='unresolved'
                    ORDER BY c.created_at,c.conflict_id""",
                (row["candidate_id"], row["candidate_version"]),
            ).fetchall()
            if len(conflict) > 1:
                diagnostics.append("conflict_integrity_failure")
                continue
            if conflict:
                warnings.append(MemoryConflictWarning(
                    conflict_id=conflict[0]["conflict_id"], fact_id=row["fact_id"],
                    canonical_key=conflict[0]["canonical_key"], risk_level=conflict[0]["risk_level"],
                ))
                continue
            ranking = _ranking_signals(provenance)
            facts.append(RetrievedMemoryFact(
                fact_id=row["fact_id"], fact_key=row["fact_key"], version=row["fact_version"],
                operator_id=operator_id, candidate_id=row["candidate_id"], candidate_version=row["candidate_version"],
                target_type=row["target_type"], subject_type=row["subject_type"], subject_id=row["subject_id"],
                predicate=row["predicate"], value=_json(row["value_json"], None),
                scope_type=row["scope_type"], scope_id=row["scope_id"], source_type=row["source_type"],
                trust_tier=row["trust_tier"], confidence=float(row["confidence"]),
                evidence_count=len(_json(row["evidence_refs_json"], [])), valid_from=valid_from,
                approved_valid_to=valid_to,
                attributed_outcome_score=ranking["attributed_outcome_score"],
                conflict_penalty=ranking["conflict_penalty"],
            ))
        return facts, warnings, sorted(set(diagnostics))

    @staticmethod
    def _canonical_head_valid(
        conn: sqlite3.Connection,
        key_rows: list[sqlite3.Row],
    ) -> bool:
        """Require one current active fact and an exact canonical head binding."""
        if not key_rows:
            return False
        key = key_rows[0]["fact_key"]
        head_id = key_rows[0]["head_id"]
        head_version = key_rows[0]["head_version"]
        current_active: list[sqlite3.Row] = []
        for row in key_rows:
            latest = conn.execute(
                """SELECT state FROM memory_fact_state_events
                    WHERE fact_id=?
                    ORDER BY effective_at DESC,created_at DESC,event_seq DESC
                    LIMIT 1""",
                (row["fact_id"],),
            ).fetchone()
            if not latest or latest["state"] not in {"active", "superseded", "revoked"}:
                return False
            if latest["state"] == "active":
                current_active.append(row)
        # Revocation intentionally removes the head. That is an inactive key,
        # not corrupted canonical state; an active fact without a head is invalid.
        if not current_active:
            return all(row["head_id"] is None for row in key_rows)
        if len(current_active) != 1 or head_id is None or any(row["head_key"] != key for row in key_rows):
            return False
        active = current_active[0]
        highest_active_version = max(row["fact_version"] for row in current_active)
        return bool(
            active["fact_version"] == highest_active_version
            and active["fact_key"] == key
            and active["fact_id"] == head_id
            and active["fact_version"] == head_version
        )

    @staticmethod
    def _state_active_at(conn: sqlite3.Connection, row: sqlite3.Row, at: datetime) -> bool:
        events = conn.execute(
            """SELECT state,effective_at FROM memory_fact_state_events
               WHERE fact_id=? AND effective_at<=?
               ORDER BY effective_at DESC,created_at DESC,event_seq DESC LIMIT 1""",
            (row["fact_id"], at.isoformat()),
        ).fetchall()
        return (
            len(events) == 1
            and _parse_time(events[0]["effective_at"]) is not None
            and events[0]["state"] == "active"
        )


def _scope_authorized(row: sqlite3.Row, request: MemoryRetrievalRequest) -> bool:
    if row["subject_type"] == "task" and (
        not request.task_type or not row["subject_id"] or row["subject_id"] != request.task_type
    ):
        return False
    if row["subject_type"] == "surface" and (
        not row["subject_id"] or row["subject_id"] != request.surface
    ):
        return False
    scope_type, scope_id = row["scope_type"], row["scope_id"]
    if scope_type == "global":
        return True
    if scope_type == "organization":
        return bool(request.organization_id and scope_id == request.organization_id)
    field = _SCOPE_FIELDS.get(scope_type)
    if field is None:
        return False
    authorized_id = getattr(request, field)
    return bool(authorized_id and scope_id == authorized_id)


def _json(value: str, fallback):
    try:
        return json.loads(value)
    except (TypeError, json.JSONDecodeError):
        return fallback


def _ranking_signals(provenance: dict[str, Any]) -> dict[str, float]:
    """Read optional WP-10-ready signals without creating or updating them."""
    raw = provenance.get("ranking_signals") if isinstance(provenance, dict) else None
    raw = raw if isinstance(raw, dict) else {}
    return {
        "attributed_outcome_score": _finite_float(raw.get("attributed_outcome_score")),
        "conflict_penalty": max(0.0, _finite_float(raw.get("conflict_penalty"))),
    }


def _finite_float(value: Any) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return 0.0
    return parsed if parsed == parsed and abs(parsed) != float("inf") else 0.0


def _parse_time(value: str | datetime | None) -> datetime | None:
    if value is None:
        return None
    parsed = value if isinstance(value, datetime) else datetime.fromisoformat(value.replace("Z", "+00:00"))
    if (
        parsed.tzinfo is None
        or parsed.utcoffset() is None
        or parsed.utcoffset().total_seconds() != 0
    ):
        raise ValueError("canonical memory timestamps must be aware UTC")
    return parsed
