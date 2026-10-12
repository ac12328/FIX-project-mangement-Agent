"""Append-only authorized lifecycle review resolution workflow."""
from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from src.db.connection import scoped_write
from src.learning.governance.revoke_service import governed_deactivate
from src.learning.lifecycle_service import append_revalidation
from src.learning.operation_service import create_operation, transition_operation
from src.learning.rollback_service import create_rollback_work

_ALLOWED = {"memory_governor", "security_admin"}
Resolution = Literal["revalidate", "quarantine", "revoke", "supersede"]


class UnsupportedReviewResolution(ValueError): pass


class ReviewResolutionRequest(BaseModel):
    model_config = ConfigDict(frozen=True)
    review_id: str
    resolution: Resolution
    actor_id: str = Field(min_length=1)
    actor_role: str = Field(min_length=1)
    reason: str = Field(min_length=1)
    idempotency_key: str = Field(min_length=1)
    at: datetime
    replacement_candidate_id: str | None = None
    replacement_candidate_version: int | None = None
    engine: Any = None


def resolve_review(request: ReviewResolutionRequest) -> str:
    at = _utc(request.at)
    if request.actor_role not in _ALLOWED:
        raise PermissionError("actor role is not authorized to resolve lifecycle reviews")
    if request.resolution == "supersede":
        if not request.replacement_candidate_id or not request.replacement_candidate_version:
            raise UnsupportedReviewResolution("supersede requires a replacement candidate and version")
        return _supersede(request)
    with scoped_write() as conn:
        existing = conn.execute("SELECT status,payload_json FROM memory_lifecycle_review_events WHERE idempotency_key=?", (request.idempotency_key,)).fetchone()
        if existing:
            if json.loads(existing["payload_json"]).get("request_hash") != _hash(request):
                raise ValueError("idempotency key reused with different review resolution")
            return existing["status"]
        review = _pending_review(conn, request.review_id)
        operation_id = None
        if request.resolution == "revalidate":
            append_revalidation(conn, fact_id=review["fact_id"], actor_id=request.actor_id, actor_role=request.actor_role,
                reason=request.reason, at=at, source_outcome_ids=_evidence_ids(conn, review),
                idempotency_key=f"review-revalidate:{request.idempotency_key}")
            status = "revalidated"
        else:
            operation_id = "mop_" + hashlib.sha256(request.idempotency_key.encode()).hexdigest()[:24]
            create_operation(conn, operation_id=operation_id, operation_type="review_resolution",
                idempotency_key=f"rollback:{request.idempotency_key}", request_hash=_hash(request), actor_id=request.actor_id,
                actor_role=request.actor_role, reason=request.reason, effective_at=at)
            decision, projection = governed_deactivate(conn, fact_id=review["fact_id"], actor_id=request.actor_id,
                reason=("quarantine pending operator review: " if request.resolution == "quarantine" else "") + request.reason,
                decided_at=at, decision_type="revoke", operation_id=operation_id)
            if decision: create_rollback_work(conn, operation_id=operation_id, fact_id=review["fact_id"], projection_event_id=projection)
            status = "quarantined" if request.resolution == "quarantine" else "revoked"
        _close(conn, request, review, status=status, operation_id=operation_id, at=at)
        return status


def _supersede(request: ReviewResolutionRequest) -> str:
    from src.learning.governance.approval import ApprovalRequest, supersede_fact
    at = _utc(request.at)
    operation_id = "mop_" + hashlib.sha256(("review-supersede:" + request.idempotency_key).encode()).hexdigest()[:24]
    with scoped_write() as conn:
        review = _pending_review(conn, request.review_id)
        operation = conn.execute("SELECT request_hash,result_json FROM memory_lifecycle_operations WHERE operation_id=?", (operation_id,)).fetchone()
        if operation and operation["request_hash"] != _hash(request):
            raise ValueError("idempotency key reused with different review resolution")
        if not operation:
            create_operation(conn, operation_id=operation_id, operation_type="supersession",
                idempotency_key=f"review-supersede:{request.idempotency_key}", request_hash=_hash(request),
                actor_id=request.actor_id, actor_role=request.actor_role, reason=request.reason, effective_at=at)
            replacement_fact_id = None
        else:
            replacement_fact_id = json.loads(operation["result_json"] or "{}").get("replacement_fact_id")
            if not replacement_fact_id:
                decision_id = "md_" + hashlib.sha256(operation_id.encode()).hexdigest()[:24]
                recovered = conn.execute("SELECT fact_id FROM memory_facts WHERE decision_id=?", (decision_id,)).fetchone()
                replacement_fact_id = recovered[0] if recovered else None
    if not replacement_fact_id:
        result = supersede_fact(ApprovalRequest(candidate_id=request.replacement_candidate_id,
            candidate_version=request.replacement_candidate_version, actor_id=request.actor_id, reason=request.reason,
            decided_at=request.at, expected_replaced_fact_id=review["fact_id"], decision_type="supersede",
            operation_id=operation_id, engine=request.engine))
        replacement_fact_id = result.fact.fact_id
        _after_supersede_commit(operation_id, replacement_fact_id)
        with scoped_write() as conn:
            conn.execute("UPDATE memory_lifecycle_operations SET result_json=? WHERE operation_id=?",
                         (json.dumps({"replacement_fact_id": replacement_fact_id}, sort_keys=True), operation_id))
    with scoped_write() as conn:
        _close(conn, request, review, status="superseded", operation_id=operation_id, at=at,
               extra={"replacement_fact_id": replacement_fact_id})
        transition_operation(conn, operation_id=operation_id, status="completed",
            reason="replacement fact persisted and review closed", at=at,
            payload={"replacement_fact_id": replacement_fact_id})
    return "superseded"


def _after_supersede_commit(operation_id: str, replacement_fact_id: str) -> None:
    """Test seam for a process crash after WP08 commits."""


def _pending_review(conn, review_id):
    review = conn.execute("SELECT * FROM memory_lifecycle_reviews WHERE review_id=?", (review_id,)).fetchone()
    if not review: raise KeyError(review_id)
    latest = conn.execute("SELECT status FROM memory_lifecycle_review_events WHERE review_id=? ORDER BY effective_at DESC,event_id DESC LIMIT 1", (review_id,)).fetchone()
    if not latest or latest["status"] != "pending": raise ValueError("review is already resolved")
    return review


def _close(conn, request, review, *, status, operation_id, at, extra=None):
    payload = {"request_hash": _hash(request), "operation_id": operation_id, **(extra or {})}
    conn.execute("""INSERT INTO memory_lifecycle_review_events
      (event_id,review_id,status,actor_id,reason,effective_at,idempotency_key,payload_json,created_at)
      VALUES (?,?,?,?,?,?,?,?,?)""", ("mlre_" + hashlib.sha256(request.idempotency_key.encode()).hexdigest()[:24],
      request.review_id, status, request.actor_id, request.reason, at.isoformat(), request.idempotency_key,
      json.dumps(payload, sort_keys=True), at.isoformat()))
    conn.execute("""INSERT INTO memory_lifecycle_events
      (event_id,fact_id,event_type,reason_code,effective_at,idempotency_key,payload_json,created_at)
      VALUES (?,?,'review_resolved',?,?,?,?,?)""", ("mle_" + hashlib.sha256(("resolved:" + request.idempotency_key).encode()).hexdigest()[:24],
      review["fact_id"], request.resolution, at.isoformat(), f"resolved:{request.idempotency_key}",
      json.dumps(payload, sort_keys=True), at.isoformat()))


def _evidence_ids(conn, review) -> list[str]:
    original = [item["outcome_id"] for item in json.loads(review["evidence_json"]) if item.get("outcome_id")]
    appended = [row[0] for row in conn.execute(
        "SELECT outcome_id FROM memory_lifecycle_review_evidence WHERE review_id=? ORDER BY effective_at,event_id",
        (review["review_id"],)).fetchall()]
    return sorted(set(original + appended))


def _hash(request: ReviewResolutionRequest) -> str:
    return hashlib.sha256(request.model_dump_json(exclude={"idempotency_key", "engine"}).encode()).hexdigest()


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None: raise ValueError("at must be timezone-aware")
    return value.astimezone(timezone.utc)
