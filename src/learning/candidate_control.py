"""Governed candidate actions used by Learning Center adapters."""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from pydantic import BaseModel, ConfigDict

from src.db.connection import scoped_read, scoped_write
from src.learning.candidate_merge_service import merge_candidates
from src.learning.control_authorization import authorize, resolve_candidate
from src.learning.evaluation_attestation import persisted_evaluation_engine
from src.learning.evaluation.runner import EvaluationPolicy, evaluation_readiness
from src.learning.governance.approval import (
    ApprovalRequest, approve_candidate, edit_and_approve_candidate, narrow_scope_candidate,
    quarantine_candidate, reject_candidate,
)
from src.learning.governance.contracts import CandidateRevision
from src.learning.governance.revision_service import append_operator_revision
from src.learning.governance.validator import load_candidate, validate_candidate
from src.repositories.personal_work_item_repo import create_personal_work_item


class CandidateControlResult(BaseModel):
    model_config = ConfigDict(frozen=True)
    candidate_id: str
    candidate_version: int
    status: str
    decision_id: str | None = None
    fact_id: str | None = None
    work_item_id: str | None = None
    conflict_id: str | None = None


def current_revision(candidate_id: str, candidate_version: int, *, actor_id: str) -> tuple[str, str, str, int]:
    with scoped_read() as conn:
        authorize(resolve_candidate(conn, candidate_id, candidate_version), operator_id=actor_id)
        row = conn.execute(
            """SELECT g.disposition,g.validation_id,v.risk_level,c.scope_type FROM memory_candidates c
               JOIN candidate_governance_states g USING(candidate_id,candidate_version)
               JOIN candidate_validation_runs v ON v.validation_id=g.validation_id
               WHERE c.candidate_id=? AND c.candidate_version=?""", (candidate_id, candidate_version),
        ).fetchone()
        if not row: raise KeyError(candidate_id)
        return (
            f"{candidate_id}:{candidate_version}:{row['validation_id']}:{row['disposition']}",
            row["risk_level"],
            row["scope_type"],
            candidate_version,
        )


def act(candidate_id: str, candidate_version: int, *, action: str, actor_id: str, reason: str,
        idempotency_key: str, proposed_value: Any = None, scope_type: str | None = None,
        scope_id: str | None = None, suggested_expiry: str | None = None,
        merge_candidate_id: str | None = None, merge_candidate_version: int | None = None) -> CandidateControlResult:
    with scoped_read() as conn:
        authorize(resolve_candidate(conn, candidate_id, candidate_version), operator_id=actor_id)
    now = datetime.now(timezone.utc)
    if action == "approve":
        result = approve_candidate(_approval(candidate_id, candidate_version, actor_id, reason, idempotency_key, now))
        return CandidateControlResult(candidate_id=candidate_id, candidate_version=candidate_version,
            status="approved", decision_id=result.decision.decision_id, fact_id=result.fact.fact_id)
    if action in {"reject", "quarantine"}:
        service = reject_candidate if action == "reject" else quarantine_candidate
        decision = service(candidate_id, candidate_version, actor_id=actor_id, reason=reason, decided_at=now)
        return CandidateControlResult(candidate_id=candidate_id, candidate_version=candidate_version,
            status=action, decision_id=decision.decision_id)
    if action == "request_evidence":
        with scoped_write() as conn:
            authorize(resolve_candidate(conn, candidate_id, candidate_version), operator_id=actor_id)
            item = create_personal_work_item(conn, kind="task", title=f"Provide evidence for {candidate_id} v{candidate_version}",
                operator_id=actor_id, description=reason, source_type="agent", source_ref=f"candidate:{candidate_id}:{candidate_version}",
                evidence_text=reason, review_status="confirmed", dedup_key=f"learning-evidence:{candidate_id}:{candidate_version}:{idempotency_key}", commit=False)
        return CandidateControlResult(candidate_id=candidate_id, candidate_version=candidate_version,
            status="evidence_requested", work_item_id=item.work_item_id)
    if action in {"edit_and_approve", "narrow_scope"}:
        revised = _revision(candidate_id, candidate_version, actor_id=actor_id, reason=reason,
            proposed_value=proposed_value, scope_type=scope_type, scope_id=scope_id, suggested_expiry=suggested_expiry)
        with scoped_write() as conn:
            validation = validate_candidate(conn, revised.candidate_id, revised.candidate_version, validation_at=now)
        if not validation.passed or validation.disposition != "needs_review":
            return CandidateControlResult(candidate_id=candidate_id, candidate_version=revised.candidate_version,
                status=validation.disposition)
        readiness = evaluation_readiness(candidate_id, revised.candidate_version, policy=EvaluationPolicy(), at=now)
        if validation.requires_evaluation and not readiness.ready:
            return CandidateControlResult(candidate_id=candidate_id, candidate_version=revised.candidate_version,
                status="needs_evaluation")
        request = _approval(candidate_id, revised.candidate_version, actor_id, reason, idempotency_key, now)
        service = edit_and_approve_candidate if action == "edit_and_approve" else narrow_scope_candidate
        result = service(request)
        return CandidateControlResult(candidate_id=candidate_id, candidate_version=revised.candidate_version,
            status="approved", decision_id=result.decision.decision_id, fact_id=result.fact.fact_id)
    if action == "merge":
        if not merge_candidate_id or not merge_candidate_version:
            raise ValueError("merge requires a candidate id and version")
        with scoped_write() as conn:
            authorize(resolve_candidate(conn, candidate_id, candidate_version), operator_id=actor_id)
            authorize(resolve_candidate(conn, merge_candidate_id, merge_candidate_version), operator_id=actor_id)
            merged = merge_candidates(
                conn,
                canonical_id=candidate_id,
                canonical_version=candidate_version,
                merged_id=merge_candidate_id,
                merged_version=merge_candidate_version,
                actor_id=actor_id,
                reason=reason,
                idempotency_key=idempotency_key,
            )
        return CandidateControlResult(
            candidate_id=candidate_id,
            candidate_version=candidate_version,
            status=merged.status,
            decision_id=merged.decision_id,
            conflict_id=merged.conflict_id,
        )
    raise ValueError(f"unsupported candidate action: {action}")


def _approval(candidate_id, version, actor_id, reason, key, now):
    return ApprovalRequest(candidate_id=candidate_id, candidate_version=version, actor_id=actor_id,
        reason=reason, decided_at=now, operation_id=key,
        engine=persisted_evaluation_engine(candidate_id, version))


def _revision(candidate_id, version, *, actor_id, reason, proposed_value, scope_type, scope_id, suggested_expiry):
    with scoped_write() as conn:
        authorize(resolve_candidate(conn, candidate_id, version), operator_id=actor_id)
        state = conn.execute("SELECT validation_id FROM candidate_governance_states WHERE candidate_id=? AND candidate_version=?", (candidate_id, version)).fetchone()
        current = load_candidate(conn, candidate_id, version)
        if not state: raise KeyError(candidate_id)
        revision = CandidateRevision(proposed_value=current.proposed_value if proposed_value is None else proposed_value,
            reasoning_summary=reason, scope_type=scope_type or current.scope_type,
            scope_id=scope_id if scope_type else current.scope_id, suggested_expiry=suggested_expiry or current.suggested_expiry)
        return append_operator_revision(conn, candidate_id, version, revision, source_validation_id=state[0])
