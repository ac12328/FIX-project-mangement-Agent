"""Deterministic governed-memory lifecycle orchestration."""
from __future__ import annotations

import hashlib
import json
from datetime import datetime, timedelta, timezone

from pydantic import BaseModel, ConfigDict, Field

from src.db.connection import scoped_read, scoped_write
from src.learning.evaluation.outcome_scoring import score_outcome
from src.learning.governance.revoke_service import governed_deactivate
from src.learning.lifecycle_policy import PolicyIntegrityError, load_bound_policy
from src.learning.operation_service import create_operation
from src.learning.rollback_service import (
    create_rollback_work,
    process_rollback_work,
    sync_projection_rollbacks,
)


class LifecycleSummary(BaseModel):
    model_config = ConfigDict(frozen=True)
    expired: int = 0
    review_required: int = 0
    revalidated: int = 0


class LifecycleStatus(BaseModel):
    model_config = ConfigDict(frozen=True)
    active_facts: int = Field(ge=0)
    pending_reviews: int = Field(ge=0)
    pending_rollback_deliveries: int = Field(ge=0)
    dead_letter_rollback_deliveries: int = Field(ge=0)
    oldest_rollback_age_seconds: int = Field(ge=0)
    rollback_slo_seconds: int = Field(ge=1)
    rollback_within_slo: bool
    operations: list[dict] = Field(default_factory=list)


def run_lifecycle(*, at: datetime | None = None) -> LifecycleSummary:
    """Evaluate governed lifecycle policy using short database-only work."""
    now = _utc(at or datetime.now(timezone.utc))
    expired = reviews = revalidated = 0
    with scoped_write() as conn:
        rows = conn.execute("""SELECT f.fact_id,f.fact_version,f.target_type,f.valid_to,v.risk_level,f.valid_from
          FROM memory_fact_heads h JOIN memory_facts f ON f.fact_id=h.fact_id
          JOIN candidate_validation_runs v ON v.validation_id=f.validation_id ORDER BY f.fact_key""").fetchall()
        for row in rows:
            try:
                policy = load_bound_policy(conn, row["fact_id"])
            except PolicyIntegrityError as exc:
                reviews += _open_integrity_review(conn, row, now, str(exc))
                continue
            validity = _current_validity(conn, row["fact_id"], row["valid_to"], row["valid_from"], policy.review_interval_seconds)
            if policy.expiry_required and validity["valid_to"] <= now:
                expired += _expire(conn, row, now); continue
            outcomes = conn.execute("""SELECT outcome_id,outcome_type,outcome_value,created_at FROM learning_outcomes
              WHERE fact_id=? AND fact_version=? ORDER BY created_at,outcome_id""", (row["fact_id"], row["fact_version"])).fetchall()
            negative = [item for item in outcomes if score_outcome(item["outcome_type"], item["outcome_value"]).score < 0]
            if len(negative) >= policy.negative_review_threshold:
                reviews += _open_review(conn, row, negative, now, policy.high_risk_behavior); continue
            if validity["next_review_at"] <= now:
                reviews += _open_review(conn, row, [], now, policy.high_risk_behavior, reason="periodic_review_due"); continue
            positive = [item for item in outcomes if score_outcome(item["outcome_type"], item["outcome_value"]).reinforces]
            if positive:
                revalidated += _revalidate(conn, row, positive, now, policy)
    return LifecycleSummary(expired=expired, review_required=reviews, revalidated=revalidated)


def run_lifecycle_maintenance(*, at: datetime | None = None, max_targets: int = 50) -> LifecycleSummary:
    """Run the production maintenance sequence without embedding lifecycle policy."""
    now = _utc(at or datetime.now(timezone.utc))
    summary = run_lifecycle() if at is None else run_lifecycle(at=now)
    from src.memory.projections import process_projection_outbox
    process_projection_outbox(max_deliveries=max_targets, at=now)
    with scoped_write() as conn:
        sync_projection_rollbacks(conn, at=now)
    process_rollback_work(max_targets=max_targets, at=now)
    with scoped_write() as conn:
        sync_projection_rollbacks(conn, at=now)
    return summary


def append_revalidation(conn, *, fact_id: str, actor_id: str, actor_role: str, reason: str,
                        at: datetime, source_outcome_ids: list[str], idempotency_key: str) -> bool:
    fact = conn.execute("SELECT target_type,valid_to,valid_from FROM memory_facts WHERE fact_id=?", (fact_id,)).fetchone()
    if not fact:
        raise KeyError(fact_id)
    policy = load_bound_policy(conn, fact_id)
    current = _current_validity(conn, fact_id, fact["valid_to"], fact["valid_from"], policy.review_interval_seconds)
    valid_to = max(current["valid_to"], at + timedelta(seconds=policy.default_ttl_seconds))
    if policy.max_revalidation_horizon_seconds is not None:
        valid_to = min(valid_to, at + timedelta(seconds=policy.max_revalidation_horizon_seconds))
    next_review = at + timedelta(seconds=policy.review_interval_seconds)
    cursor = conn.execute("""INSERT OR IGNORE INTO memory_fact_validity_events
      (event_id,fact_id,event_type,valid_to,next_review_at,actor_id,actor_role,reason,source_outcome_ids_json,effective_at,idempotency_key,created_at)
      VALUES (?,?,'revalidated',?,?,?,?,?,?,?,?,?)""", (_id("mfv", idempotency_key), fact_id, valid_to.isoformat(), next_review.isoformat(),
      actor_id, actor_role, reason, json.dumps(sorted(source_outcome_ids)), at.isoformat(), idempotency_key, at.isoformat()))
    return cursor.rowcount == 1


def _expire(conn, row, at: datetime) -> int:
    key = f"expiry:{row['fact_id']}:{row['fact_version']}"
    if conn.execute("SELECT 1 FROM memory_lifecycle_events WHERE idempotency_key=?", (key,)).fetchone(): return 0
    operation_id = _id("mop", key)
    create_operation(conn, operation_id=operation_id, operation_type="expiry", idempotency_key=key,
        request_hash=hashlib.sha256(key.encode()).hexdigest(), actor_id="lifecycle", actor_role="system_lifecycle",
        reason="approved validity expired", effective_at=at)
    decision, projection = governed_deactivate(conn, fact_id=row["fact_id"], actor_id="lifecycle",
        reason="system expiry: approved validity expired", decided_at=at, decision_type="expire", operation_id=operation_id)
    if decision is None: return 0
    create_rollback_work(conn, operation_id=operation_id, fact_id=row["fact_id"], projection_event_id=projection)
    conn.execute("""INSERT INTO memory_lifecycle_events
      (event_id,fact_id,event_type,reason_code,effective_at,idempotency_key,payload_json,created_at)
      VALUES (?,?,'expired','approved_validity_expired',?,?,?,?)""", (_id("mle", key), row["fact_id"], at.isoformat(), key,
      json.dumps({"decision_id": decision.decision_id, "operation_id": operation_id}, sort_keys=True), at.isoformat()))
    return 1


def _open_review(conn, row, negative, at: datetime, behavior: str, *, reason: str = "negative_exact_outcome") -> int:
    outcome_ids = sorted({item["outcome_id"] for item in negative})
    policy = load_bound_policy(conn, row["fact_id"])
    policy_version = policy.policy_version
    validity = _current_validity(
        conn, row["fact_id"], row["valid_to"], row["valid_from"], policy.review_interval_seconds,
    )
    due_boundary = validity["next_review_at"].isoformat()
    evidence_key = "|".join(outcome_ids) if outcome_ids else f"due:{due_boundary}:{policy_version}"
    evidence_hash = hashlib.sha256(evidence_key.encode()).hexdigest()
    pending = conn.execute("""SELECT r.review_id,r.evidence_json FROM memory_lifecycle_reviews r
      WHERE r.fact_id=? AND r.fact_version=? AND r.reason_code=? AND
      (SELECT status FROM memory_lifecycle_review_events e WHERE e.review_id=r.review_id
       ORDER BY effective_at DESC,event_id DESC LIMIT 1)='pending' ORDER BY r.generation DESC LIMIT 1""",
      (row["fact_id"], row["fact_version"], reason)).fetchone()
    if pending:
        previous = {item.get("outcome_id") for item in json.loads(pending["evidence_json"]) if item.get("outcome_id")}
        previous.update(item[0] for item in conn.execute(
            "SELECT outcome_id FROM memory_lifecycle_review_evidence WHERE review_id=?", (pending["review_id"],)))
        new_ids = sorted(set(outcome_ids) - previous)
        for outcome_id in new_ids:
            key = f"review-evidence:{pending['review_id']}:{outcome_id}"
            conn.execute("""INSERT OR IGNORE INTO memory_lifecycle_review_evidence
              (event_id,review_id,outcome_id,effective_at,idempotency_key,created_at)
              VALUES (?,?,?,?,?,?)""", (_id("mlrev", key), pending["review_id"], outcome_id,
              at.isoformat(), key, at.isoformat()))
            conn.execute("""INSERT OR IGNORE INTO memory_lifecycle_events
              (event_id,fact_id,event_type,reason_code,source_outcome_id,effective_at,idempotency_key,payload_json,created_at)
              VALUES (?,?,'review_required',?,?,?,?,?,?)""", (_id("mle", key), row["fact_id"], reason,
              outcome_id, at.isoformat(), key, json.dumps({"review_id": pending["review_id"], "appended_outcome_ids": [outcome_id]}, sort_keys=True), at.isoformat()))
        return 0
    existing = conn.execute("SELECT review_id FROM memory_lifecycle_reviews WHERE fact_id=? AND fact_version=? AND reason_code=? AND evidence_hash=?",
        (row["fact_id"], row["fact_version"], reason, evidence_hash)).fetchone()
    if existing: return 0
    generation = conn.execute("SELECT COALESCE(MAX(generation),0)+1 FROM memory_lifecycle_reviews WHERE fact_id=? AND fact_version=?",
                              (row["fact_id"], row["fact_version"])).fetchone()[0]
    review_id = _id("mlr", row["fact_id"], row["fact_version"], reason, evidence_hash)
    evidence = [{"outcome_id": item["outcome_id"], "type": item["outcome_type"]} for item in negative]
    conn.execute("INSERT INTO memory_lifecycle_reviews(review_id,fact_id,fact_version,generation,reason_code,evidence_hash,evidence_json,opened_at) VALUES (?,?,?,?,?,?,?,?)",
        (review_id, row["fact_id"], row["fact_version"], generation, reason, evidence_hash, json.dumps(evidence, sort_keys=True), at.isoformat()))
    conn.execute("""INSERT INTO memory_lifecycle_review_events
      (event_id,review_id,status,actor_id,reason,effective_at,idempotency_key,payload_json,created_at)
      VALUES (?,?,'pending','lifecycle',?,?,?,?,?)""", (_id("mlre", review_id, "pending"), review_id, reason, at.isoformat(),
      f"review-open:{review_id}", json.dumps({"outcome_ids": outcome_ids, "evidence_hash": evidence_hash}, sort_keys=True), at.isoformat()))
    conn.execute("""INSERT INTO memory_lifecycle_events
      (event_id,fact_id,event_type,reason_code,source_outcome_id,effective_at,idempotency_key,payload_json,created_at)
      VALUES (?,?,'review_required',?,?,?,?,?,?)""", (_id("mle", review_id), row["fact_id"], reason,
      outcome_ids[-1] if outcome_ids else None, at.isoformat(), f"review:{review_id}",
      json.dumps({"outcome_ids": outcome_ids, "evidence_hash": evidence_hash, "due_boundary": due_boundary,
                  "policy_version": policy_version}, sort_keys=True), at.isoformat()))
    if row["risk_level"] == "high" and behavior == "quarantine_pending_review":
        _quarantine_for_review(conn, row["fact_id"], review_id, at)
    return 1


def _open_integrity_review(conn, row, at: datetime, reason: str) -> int:
    evidence_hash = hashlib.sha256(reason.encode()).hexdigest()
    existing = conn.execute("SELECT review_id FROM memory_lifecycle_reviews WHERE fact_id=? AND fact_version=? AND reason_code='policy_integrity_failure' AND evidence_hash=?",
        (row["fact_id"], row["fact_version"], evidence_hash)).fetchone()
    if existing:
        return 0
    generation = conn.execute("SELECT COALESCE(MAX(generation),0)+1 FROM memory_lifecycle_reviews WHERE fact_id=? AND fact_version=?",
                              (row["fact_id"], row["fact_version"])).fetchone()[0]
    review_id = _id("mlr", row["fact_id"], "policy-integrity", evidence_hash)
    conn.execute("INSERT INTO memory_lifecycle_reviews(review_id,fact_id,fact_version,generation,reason_code,evidence_hash,evidence_json,opened_at) VALUES (?,?,?,?,?,?,?,?)",
        (review_id, row["fact_id"], row["fact_version"], generation, "policy_integrity_failure", evidence_hash,
         json.dumps([{"error": reason}], sort_keys=True), at.isoformat()))
    conn.execute("""INSERT INTO memory_lifecycle_review_events
      (event_id,review_id,status,actor_id,reason,effective_at,idempotency_key,payload_json,created_at)
      VALUES (?,?,'pending','lifecycle',?,?,?,?,?)""", (_id("mlre", review_id, "pending"), review_id,
      "manual review required: " + reason, at.isoformat(), f"policy-integrity:{review_id}",
      json.dumps({"error": reason}, sort_keys=True), at.isoformat()))
    _quarantine_for_review(conn, row["fact_id"], review_id, at)
    return 1


def _quarantine_for_review(conn, fact_id: str, review_id: str, at: datetime) -> None:
    operation_id = _id("mop", "quarantine", review_id)
    create_operation(conn, operation_id=operation_id, operation_type="review_resolution", idempotency_key=f"quarantine:{review_id}",
        request_hash=hashlib.sha256(review_id.encode()).hexdigest(), actor_id="lifecycle", actor_role="system_lifecycle",
        reason="high-risk negative evidence; system revoke pending review", effective_at=at)
    decision, projection = governed_deactivate(conn, fact_id=fact_id, actor_id="lifecycle",
        reason="high-risk negative evidence; system revoke pending review", decided_at=at, decision_type="revoke", operation_id=operation_id)
    if decision: create_rollback_work(conn, operation_id=operation_id, fact_id=fact_id, projection_event_id=projection)


def _revalidate(conn, row, positive, at: datetime, policy) -> int:
    cutoff = at - timedelta(seconds=policy.revalidation_window_seconds)
    ids = sorted({item["outcome_id"] for item in positive if _parse_db_time(item["created_at"]) >= cutoff})
    if not ids: return 0
    evidence_hash = hashlib.sha256("|".join(ids).encode()).hexdigest(); key = f"revalidate:{row['fact_id']}:{row['fact_version']}:{evidence_hash}"
    if not append_revalidation(conn, fact_id=row["fact_id"], actor_id="lifecycle", actor_role="system_lifecycle",
        reason="positive exact outcomes", at=at, source_outcome_ids=ids, idempotency_key=key): return 0
    conn.execute("""INSERT INTO memory_lifecycle_events
      (event_id,fact_id,event_type,reason_code,source_outcome_id,effective_at,idempotency_key,payload_json,created_at)
      VALUES (?,?,'revalidated','positive_exact_outcomes',?,?,?,?,?)""", (_id("mle", key), row["fact_id"], ids[-1], at.isoformat(), key,
      json.dumps({"outcome_ids": ids, "evidence_hash": evidence_hash}, sort_keys=True), at.isoformat()))
    return 1


def get_lifecycle_status(*, at: datetime | None = None, rollback_slo_seconds: int = 300) -> LifecycleStatus:
    now = _utc(at or datetime.now(timezone.utc))
    with scoped_write() as conn:
        sync_projection_rollbacks(conn, at=now)
    with scoped_read() as conn:
        active = conn.execute("SELECT COUNT(*) FROM memory_fact_heads").fetchone()[0]
        reviews = conn.execute("""SELECT COUNT(*) FROM memory_lifecycle_reviews r WHERE
          (SELECT status FROM memory_lifecycle_review_events e WHERE e.review_id=r.review_id ORDER BY effective_at DESC,event_id DESC LIMIT 1)='pending'""").fetchone()[0]
        rows = conn.execute("""SELECT r.*,o.effective_at,o.deadline FROM memory_rollback_targets r
          JOIN memory_lifecycle_operations o USING(operation_id) ORDER BY o.effective_at,r.operation_id,r.fact_id,r.target""").fetchall()
        operations = [dict(row) for row in conn.execute("SELECT * FROM memory_lifecycle_operations ORDER BY effective_at,operation_id").fetchall()]
        events = [dict(row) for row in conn.execute("SELECT * FROM memory_lifecycle_operation_events ORDER BY operation_id,effective_at,event_id").fetchall()]
        targets = [dict(row) for row in conn.execute("SELECT * FROM memory_rollback_targets ORDER BY operation_id,fact_id,target").fetchall()]
        attempts = [dict(row) for row in conn.execute("SELECT * FROM memory_rollback_attempts ORDER BY operation_id,fact_id,target,attempt_number").fetchall()]
    events_by_op: dict[str, list[dict]] = {}
    targets_by_op: dict[str, list[dict]] = {}
    attempts_by_target: dict[tuple[str, str, str], list[dict]] = {}
    for event in events:
        events_by_op.setdefault(event["operation_id"], []).append(event)
    for attempt in attempts:
        attempts_by_target.setdefault((attempt["operation_id"], attempt["fact_id"], attempt["target"]), []).append(attempt)
    for target in targets:
        target["attempt_history"] = attempts_by_target.get((target["operation_id"], target["fact_id"], target["target"]), [])
        targets_by_op.setdefault(target["operation_id"], []).append(target)
    for operation in operations:
        operation["events"] = events_by_op.get(operation["operation_id"], [])
        operation["targets"] = targets_by_op.get(operation["operation_id"], [])
    active_rows = [row for row in rows if row["status"] != "completed"]
    ages = [max(0, int((now - _parse_db_time(row["effective_at"])).total_seconds())) for row in active_rows]
    pending = sum(row["status"] in {"pending", "partial_failed"} for row in active_rows)
    dead = sum(row["status"] in {"dead_letter", "slo_breached", "manual_review"} for row in active_rows)
    oldest = max(ages, default=0)
    return LifecycleStatus(active_facts=active, pending_reviews=reviews, pending_rollback_deliveries=pending,
        dead_letter_rollback_deliveries=dead, oldest_rollback_age_seconds=oldest, rollback_slo_seconds=rollback_slo_seconds,
        rollback_within_slo=dead == 0 and oldest <= rollback_slo_seconds, operations=operations)


def _current_validity(conn, fact_id: str, legacy_valid_to: str | None, valid_from: str, review_seconds: int) -> dict:
    row = conn.execute("SELECT valid_to,next_review_at FROM memory_fact_validity_events WHERE fact_id=? ORDER BY effective_at DESC,created_at DESC,event_id DESC LIMIT 1", (fact_id,)).fetchone()
    if row: return {"valid_to": _parse_db_time(row[0]), "next_review_at": _parse_db_time(row[1])}
    return {"valid_to": _parse_db_time(legacy_valid_to), "next_review_at": _parse_db_time(valid_from) + timedelta(seconds=review_seconds)}


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None: raise ValueError("lifecycle time must be timezone-aware")
    return value.astimezone(timezone.utc)


def _parse_db_time(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None: parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _id(prefix: str, *parts) -> str:
    return f"{prefix}_{hashlib.sha256('|'.join(map(str, parts)).encode()).hexdigest()[:24]}"


from src.learning.emergency_revoke import EmergencyRevokeRequest, EmergencyRevokeResult, emergency_revoke
