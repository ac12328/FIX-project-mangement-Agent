"""Public deterministic approval services owning evaluation and write scopes."""
from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from src.db.connection import scoped_write
from src.learning.evaluation.runner import EvaluationPolicy, ReplayEngine, effective_required_suites, evaluation_readiness
from src.memory.fact_store import enqueue_projection, fact_key, materialize
from src.repositories.memory_candidate_repo import governed_candidate
from src.repositories.memory_conflict_repo import is_authority_blocked
from src.repositories.memory_decision_repo import MemoryDecision, insert_decision
from src.repositories.memory_fact_repo import MemoryFact, append_state, get_active_fact, get_fact


class ApprovalError(ValueError): pass


class ApprovalRequest(BaseModel):
    model_config = ConfigDict(frozen=True, arbitrary_types_allowed=True)
    candidate_id: str
    candidate_version: int = Field(ge=1)
    actor_id: str
    reason: str
    decided_at: datetime
    engine: Any = None
    evaluation_policy: EvaluationPolicy = Field(default_factory=EvaluationPolicy)
    expected_replaced_fact_id: str | None = None
    decision_type: Literal["approve", "edit_and_approve", "narrow_scope", "supersede"] = "approve"
    operation_id: str | None = None


class ApprovalResult(BaseModel):
    model_config = ConfigDict(frozen=True)
    decision: MemoryDecision
    fact: MemoryFact


def _at(value: datetime) -> datetime:
    if value.tzinfo is None: raise ApprovalError("decision time must be timezone-aware")
    return value.astimezone(timezone.utc)



def _validate_event_time(value: datetime, *, activated_at: datetime, latest_event_at: datetime) -> datetime:
    at = _at(value)
    if at < _at(activated_at) or at < _at(latest_event_at):
        raise ApprovalError("decision chronology precedes activation or latest event")
    return at


def _decision(request: ApprovalRequest, validation_id: str, evaluation_id: str | None) -> MemoryDecision:
    if not request.actor_id.strip() or not request.reason.strip():
        raise ApprovalError("human actor identity and explicit reason are required")
    decision_id = ("md_" + __import__("hashlib").sha256(request.operation_id.encode()).hexdigest()[:24]
                   if request.operation_id else f"md_{uuid.uuid4().hex}")
    return MemoryDecision(decision_id=decision_id, candidate_id=request.candidate_id,
        candidate_version=request.candidate_version, decision_type=request.decision_type, actor_id=request.actor_id,
        reason=request.reason, validation_id=validation_id, evaluation_id=evaluation_id,
        replaces_fact_id=request.expected_replaced_fact_id, decision_payload_json="{}", decided_at=_at(request.decided_at))


def _requires_evaluation(candidate: dict, policy: EvaluationPolicy) -> bool:
    snapshot = {"candidate": candidate, "risk_level": candidate["risk_level"]}
    return bool(effective_required_suites(snapshot, policy))


def approve_candidate(request: ApprovalRequest) -> ApprovalResult:
    readiness = evaluation_readiness(request.candidate_id, request.candidate_version,
        policy=request.evaluation_policy, at=_at(request.decided_at), engine=request.engine)
    with scoped_write() as conn:
        if request.operation_id:
            decision_id = "md_" + __import__("hashlib").sha256(request.operation_id.encode()).hexdigest()[:24]
            prior = conn.execute("SELECT f.fact_id FROM memory_decisions d JOIN memory_facts f ON f.decision_id=d.decision_id WHERE d.decision_id=?", (decision_id,)).fetchone()
            if prior:
                return ApprovalResult(decision=MemoryDecision(**dict(conn.execute("SELECT * FROM memory_decisions WHERE decision_id=?", (decision_id,)).fetchone())), fact=get_fact(conn, prior[0]))
        candidate = governed_candidate(conn, request.candidate_id, request.candidate_version)
        if not candidate["passed"] or candidate["disposition"] != "needs_review":
            raise ApprovalError("current passing validation and review disposition are required")
        observation = conn.execute(
            "SELECT source_id,content_hash,raw_payload_json FROM learning_observations WHERE observation_id=?",
            (candidate["observation_id"],),
        ).fetchone()
        from src.learning.authority import authority_denied
        denied = authority_denied(conn, observation)
        if denied:
            raise ApprovalError(denied)
        if is_authority_blocked(conn, request.candidate_id, request.candidate_version):
            raise ApprovalError("unresolved authority-blocking conflict")
        required = _requires_evaluation(candidate, request.evaluation_policy)
        if required and (not readiness.ready or not readiness.evaluation_id):
            raise ApprovalError("current canonical evaluation readiness failed: " + ",".join(readiness.reasons))
        evaluation_id = readiness.evaluation_id if required else None
        if evaluation_id:
            evaluation = conn.execute("SELECT * FROM candidate_evaluations WHERE evaluation_id=? AND candidate_id=? AND candidate_version=? AND passed=1", (evaluation_id, request.candidate_id, request.candidate_version)).fetchone()
            latest = conn.execute("SELECT evaluation_id FROM candidate_evaluations WHERE candidate_id=? AND candidate_version=? ORDER BY evaluated_at DESC,created_at DESC,evaluation_id DESC LIMIT 1", (request.candidate_id, request.candidate_version)).fetchone()
            if evaluation is None or latest is None or latest[0] != evaluation_id:
                raise ApprovalError("ready evaluation is no longer latest/current for candidate version")
        if request.expected_replaced_fact_id:
            replaced = get_fact(conn, request.expected_replaced_fact_id)
            latest_event = conn.execute("SELECT effective_at FROM memory_fact_state_events WHERE fact_id=? ORDER BY effective_at DESC,created_at DESC,event_seq DESC LIMIT 1", (replaced.fact_id,)).fetchone()
            _validate_event_time(request.decided_at, activated_at=replaced.valid_from, latest_event_at=datetime.fromisoformat(latest_event[0]))
        decision = _decision(request, candidate["validation_id"], evaluation_id)
        insert_decision(conn, decision)
        fact = materialize(conn, candidate, decision_id=decision.decision_id, evaluation_id=evaluation_id,
            decided_at=_at(request.decided_at), actor_id=request.actor_id, reason=request.reason,
            expected_replaced_fact_id=request.expected_replaced_fact_id)
        _audit(conn, decision, fact.fact_id)
        return ApprovalResult(decision=decision, fact=fact)


def edit_and_approve_candidate(request: ApprovalRequest) -> ApprovalResult:
    return approve_candidate(request.model_copy(update={"decision_type": "edit_and_approve"}))


def narrow_scope_candidate(request: ApprovalRequest) -> ApprovalResult:
    return approve_candidate(request.model_copy(update={"decision_type": "narrow_scope"}))


def supersede_fact(request: ApprovalRequest) -> ApprovalResult:
    if not request.expected_replaced_fact_id: raise ApprovalError("supersession requires expected fact id")
    return approve_candidate(request.model_copy(update={"decision_type": "supersede"}))


def _terminal(candidate_id: str, candidate_version: int, *, decision_type: str, actor_id: str, reason: str, decided_at: datetime):
    with scoped_write() as conn:
        candidate = governed_candidate(conn, candidate_id, candidate_version)
        request = ApprovalRequest(candidate_id=candidate_id, candidate_version=candidate_version, actor_id=actor_id,
            reason=reason, decided_at=decided_at, decision_type="approve")
        decision = _decision(request, candidate["validation_id"], None).model_copy(update={"decision_type": decision_type})
        insert_decision(conn, decision); _audit(conn, decision, None); return decision


def reject_candidate(candidate_id, candidate_version, **kwargs): return _terminal(candidate_id, candidate_version, decision_type="reject", **kwargs)
def quarantine_candidate(candidate_id, candidate_version, **kwargs): return _terminal(candidate_id, candidate_version, decision_type="quarantine", **kwargs)


def revoke_fact(fact_id: str, *, actor_id: str, reason: str, decided_at: datetime,
                conn=None, operation_id: str | None = None):
    """Revoke atomically, optionally inside a caller-owned lifecycle transaction."""
    if conn is None:
        with scoped_write() as active:
            return revoke_fact(fact_id, actor_id=actor_id, reason=reason,
                               decided_at=decided_at, conn=active, operation_id=operation_id)
    fact = get_fact(conn, fact_id)
    active = get_active_fact(conn, fact.fact_key)
    if active is None or active.fact_id != fact_id:
        raise ApprovalError("only the active fact can be revoked")
    latest_event = conn.execute("SELECT effective_at FROM memory_fact_state_events WHERE fact_id=? ORDER BY effective_at DESC,created_at DESC,event_seq DESC LIMIT 1", (fact_id,)).fetchone()
    decided_at = _validate_event_time(decided_at, activated_at=fact.valid_from,
                                     latest_event_at=datetime.fromisoformat(latest_event[0]))
    from src.learning.governance.revoke_service import governed_deactivate
    decision, _ = governed_deactivate(conn, fact_id=fact_id, actor_id=actor_id, reason=reason,
        decided_at=decided_at, decision_type="revoke", operation_id=operation_id)
    if decision is None:
        raise ApprovalError("only the active fact can be revoked")
    return decision


def _audit(conn, decision, fact_id):
    conn.execute("INSERT INTO memory_audit_events(event_id,event_type,decision_id,fact_id,payload_json,created_at) VALUES (?,?,?,?,?,?)",
        (f"ma_{uuid.uuid4().hex}", f"memory_{decision.decision_type}", decision.decision_id, fact_id, decision.decision_payload_json, decision.decided_at.isoformat()))
