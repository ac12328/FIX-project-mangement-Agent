"""Fact lifecycle actions for the Learning Center."""
from __future__ import annotations

import hashlib
from datetime import datetime, timezone

from pydantic import BaseModel, ConfigDict

from src.db.connection import scoped_read, scoped_write
from src.learning.control_authorization import authorize, resolve_fact
from src.learning.evaluation_attestation import persisted_evaluation_engine
from src.learning.governance.approval import ApprovalRequest, revoke_fact, supersede_fact
from src.learning.governance.contracts import CandidateRevision
from src.learning.governance.revision_service import append_operator_revision
from src.learning.governance.validator import load_candidate, validate_candidate
from src.learning.operation_service import create_operation
from src.learning.rollback_service import create_rollback_work


class FactControlResult(BaseModel):
    model_config = ConfigDict(frozen=True)
    fact_id: str
    status: str
    operation_id: str | None = None
    decision_id: str | None = None
    candidate_id: str | None = None
    candidate_version: int | None = None
    replacement_fact_id: str | None = None


def revision(fact_id: str, *, actor_id: str) -> str:
    with scoped_read() as conn:
        authorize(resolve_fact(conn, fact_id), operator_id=actor_id)
        row = conn.execute("SELECT fact_version,status FROM memory_facts WHERE fact_id=?", (fact_id,)).fetchone()
        if not row: raise KeyError(fact_id)
        state = conn.execute("SELECT state FROM memory_fact_state_events WHERE fact_id=? ORDER BY effective_at DESC,created_at DESC,event_seq DESC LIMIT 1", (fact_id,)).fetchone()
        return f"{fact_id}:{row['fact_version']}:{state[0] if state else row['status']}"


def deactivate(fact_id: str, *, actor_id: str, reason: str, idempotency_key: str) -> FactControlResult:
    """Commit operation, canonical revocation and rollback work as one unit."""
    if not idempotency_key.strip() or not actor_id.strip() or not reason.strip():
        raise ValueError("deactivation identity and reason are required")
    now = datetime.now(timezone.utc)
    operation_id = "mop_" + hashlib.sha256(("fact-deactivate:" + idempotency_key).encode()).hexdigest()[:24]
    request_hash = hashlib.sha256(f"{fact_id}|{reason}".encode()).hexdigest()
    with scoped_write() as conn:
        authorize(resolve_fact(conn, fact_id), operator_id=actor_id)
        existing = conn.execute("SELECT * FROM memory_lifecycle_operations WHERE operation_id=?", (operation_id,)).fetchone()
        if existing:
            if (existing["request_hash"] != request_hash or existing["actor_id"] != actor_id
                    or existing["operation_type"] != "rollback"):
                raise ValueError("deactivation idempotency identity conflict")
            decision = conn.execute(
                """SELECT decision_id FROM memory_decisions WHERE replaces_fact_id=?
                   AND decision_type='revoke' AND json_extract(decision_payload_json,'$.operation_id')=?""",
                (fact_id, operation_id),
            ).fetchone()
            if decision:
                state = conn.execute("SELECT state FROM memory_fact_state_events WHERE fact_id=? ORDER BY effective_at DESC,created_at DESC,event_seq DESC LIMIT 1", (fact_id,)).fetchone()
                if not state or state[0] != "revoked":
                    raise ValueError("deactivation receipt conflicts with current fact state")
                return FactControlResult(fact_id=fact_id, status="revoked", operation_id=operation_id,
                                         decision_id=decision[0])
            if existing["status"] != "pending":
                raise ValueError("deactivation operation lacks a committed receipt; reconciliation required")
            # Recover a legacy operation created before its revocation committed.
            # If the fact was already revoked without a bound receipt, the shared
            # active-fact guard requires reconciliation rather than guessing.
        else:
            create_operation(conn, operation_id=operation_id, operation_type="rollback", idempotency_key=idempotency_key,
                request_hash=request_hash, actor_id=actor_id, actor_role="operator", reason=reason, effective_at=now)
        decision = revoke_fact(fact_id, actor_id=actor_id, reason=reason, decided_at=now,
                               conn=conn, operation_id=operation_id)
        projection = conn.execute("SELECT event_id FROM memory_projection_outbox WHERE fact_id=? AND event_type='delete' ORDER BY created_at DESC LIMIT 1", (fact_id,)).fetchone()
        create_rollback_work(conn, operation_id=operation_id, fact_id=fact_id,
                             projection_event_id=projection[0] if projection else None)
        return FactControlResult(fact_id=fact_id, status="revoked", operation_id=operation_id,
                                 decision_id=decision.decision_id)


def create_change_candidate(fact_id: str, *, actor_id: str, reason: str, action: str,
                            scope_type: str | None = None, scope_id: str | None = None,
                            suggested_expiry: str | None = None) -> FactControlResult:
    with scoped_write() as conn:
        ownership = authorize(resolve_fact(conn, fact_id), operator_id=actor_id)
        fact = conn.execute("SELECT candidate_id,candidate_version FROM memory_facts WHERE fact_id=?", (fact_id,)).fetchone()
        candidate = load_candidate(conn, fact[0], fact[1])
        state = conn.execute("SELECT validation_id FROM candidate_governance_states WHERE candidate_id=? AND candidate_version=?", (fact[0], fact[1])).fetchone()
        revision = CandidateRevision(proposed_value=candidate.proposed_value, reasoning_summary=reason,
            scope_type=scope_type or candidate.scope_type, scope_id=scope_id if scope_type else candidate.scope_id,
            suggested_expiry=suggested_expiry or candidate.suggested_expiry)
        revised = append_operator_revision(conn, fact[0], fact[1], revision, source_validation_id=state[0])
        result = validate_candidate(conn, revised.candidate_id, revised.candidate_version,
                                    validation_at=datetime.now(timezone.utc))
    return FactControlResult(fact_id=fact_id, status=result.disposition,
        candidate_id=revised.candidate_id, candidate_version=revised.candidate_version)


def supersede(fact_id: str, *, replacement_candidate_id: str, replacement_candidate_version: int,
              actor_id: str, reason: str, idempotency_key: str) -> FactControlResult:
    with scoped_read() as conn:
        authorize(resolve_fact(conn, fact_id), operator_id=actor_id)
        authorize(__import__("src.learning.control_authorization", fromlist=["resolve_candidate"]).resolve_candidate(
            conn, replacement_candidate_id, replacement_candidate_version), operator_id=actor_id)
    operation_id = "mop_" + hashlib.sha256(("fact-supersede:" + idempotency_key).encode()).hexdigest()[:24]
    engine = persisted_evaluation_engine(replacement_candidate_id, replacement_candidate_version)
    result = supersede_fact(ApprovalRequest(candidate_id=replacement_candidate_id,
        candidate_version=replacement_candidate_version, actor_id=actor_id, reason=reason,
        decided_at=datetime.now(timezone.utc), expected_replaced_fact_id=fact_id,
        operation_id=operation_id, engine=engine))
    return FactControlResult(fact_id=fact_id, status="superseded", operation_id=operation_id,
        decision_id=result.decision.decision_id, replacement_fact_id=result.fact.fact_id)
