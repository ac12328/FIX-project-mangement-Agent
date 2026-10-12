"""Immutable persisted lifecycle policy for every governed memory class."""
from __future__ import annotations

import hashlib
import json
import sqlite3
from datetime import datetime, timedelta, timezone
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

TargetType = Literal["semantic_fact", "preference", "procedure", "wiki_publication"]
HighRiskBehavior = Literal["quarantine_pending_review", "remain_active_with_warning"]
POLICY_VERSION = "wp11-v2"


class PolicyIntegrityError(RuntimeError):
    """Persisted policy binding is missing or does not match its immutable hash."""


class LifecyclePolicy(BaseModel):
    model_config = ConfigDict(frozen=True)
    policy_version: str = POLICY_VERSION
    target_type: TargetType
    default_ttl_seconds: int = Field(gt=0)
    review_interval_seconds: int = Field(gt=0)
    revalidation_window_seconds: int = Field(gt=0)
    max_revalidation_horizon_seconds: int | None = Field(default=None, gt=0)
    expiry_required: bool = True
    negative_review_threshold: int = Field(gt=0)
    high_risk_behavior: HighRiskBehavior
    effective_from: datetime
    effective_to: datetime | None = None


_EFFECTIVE = datetime(2026, 1, 1, tzinfo=timezone.utc)
POLICIES: dict[str, LifecyclePolicy] = {
    "semantic_fact": LifecyclePolicy(target_type="semantic_fact", default_ttl_seconds=180*86400, review_interval_seconds=90*86400, revalidation_window_seconds=30*86400, negative_review_threshold=1, high_risk_behavior="quarantine_pending_review", effective_from=_EFFECTIVE),
    "preference": LifecyclePolicy(target_type="preference", default_ttl_seconds=365*86400, review_interval_seconds=180*86400, revalidation_window_seconds=60*86400, negative_review_threshold=2, high_risk_behavior="quarantine_pending_review", effective_from=_EFFECTIVE),
    "procedure": LifecyclePolicy(target_type="procedure", default_ttl_seconds=90*86400, review_interval_seconds=30*86400, revalidation_window_seconds=14*86400, negative_review_threshold=1, high_risk_behavior="quarantine_pending_review", effective_from=_EFFECTIVE),
    "wiki_publication": LifecyclePolicy(target_type="wiki_publication", default_ttl_seconds=180*86400, review_interval_seconds=60*86400, revalidation_window_seconds=30*86400, negative_review_threshold=1, high_risk_behavior="quarantine_pending_review", effective_from=_EFFECTIVE),
}


def policy_for(target_type: str) -> LifecyclePolicy:
    try:
        return POLICIES[target_type]
    except KeyError as exc:
        raise ValueError(f"unsupported governed target type: {target_type}") from exc


def _policy_payload(policy: LifecyclePolicy) -> dict:
    return policy.model_dump(mode="json", exclude={"policy_version"})


def policy_hash(policy: LifecyclePolicy) -> str:
    raw = json.dumps(_policy_payload(policy), sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode()).hexdigest()


def persist_policy_snapshot(conn: sqlite3.Connection, *, fact_id: str, target_type: str,
                            valid_from: datetime, approved_valid_to: datetime | None) -> datetime:
    policy = policy_for(target_type)
    expiry = approved_valid_to or (valid_from + timedelta(seconds=policy.default_ttl_seconds))
    conn.execute(
        """INSERT OR IGNORE INTO memory_lifecycle_policies
           (policy_version,target_type,default_ttl_seconds,review_interval_seconds,revalidation_window_seconds,
            max_revalidation_horizon_seconds,expiry_required,negative_review_threshold,high_risk_behavior,
            effective_from,effective_to,policy_hash)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
        (policy.policy_version, policy.target_type, policy.default_ttl_seconds, policy.review_interval_seconds,
         policy.revalidation_window_seconds, policy.max_revalidation_horizon_seconds, int(policy.expiry_required),
         policy.negative_review_threshold, policy.high_risk_behavior, policy.effective_from.isoformat(),
         policy.effective_to.isoformat() if policy.effective_to else None, policy_hash(policy)),
    )
    stored = conn.execute("SELECT policy_hash FROM memory_lifecycle_policies WHERE policy_version=? AND target_type=?",
                          (policy.policy_version, policy.target_type)).fetchone()
    if not stored or stored[0] != policy_hash(policy):
        raise PolicyIntegrityError("current policy version conflicts with immutable persisted snapshot")
    conn.execute(
        """INSERT INTO memory_fact_lifecycle_policy
           (fact_id,policy_version,target_type,effective_from,effective_to,approved_valid_to)
           VALUES (?,?,?,?,?,?)""",
        (fact_id, policy.policy_version, policy.target_type, valid_from.isoformat(),
         policy.effective_to.isoformat() if policy.effective_to else None, expiry.isoformat()),
    )
    next_review = valid_from + timedelta(seconds=policy.review_interval_seconds)
    key = f"validity-approved:{fact_id}"
    conn.execute(
        """INSERT OR IGNORE INTO memory_fact_validity_events
           (event_id,fact_id,event_type,valid_to,next_review_at,actor_id,actor_role,reason,
            source_outcome_ids_json,effective_at,idempotency_key,created_at)
           VALUES (?,?, 'approved',?,?, 'approval','memory_governor','initial governed validity','[]',?,?,?)""",
        (f"mfv_{fact_id}", fact_id, expiry.isoformat(), next_review.isoformat(),
         valid_from.isoformat(), key, valid_from.isoformat()),
    )
    return expiry


def bound_policy_valid(conn: sqlite3.Connection, fact_id: str) -> bool:
    """Validate a fact's immutable lifecycle binding using persisted DB values only."""
    try:
        load_bound_policy(conn, fact_id)
        return True
    except (PolicyIntegrityError, sqlite3.DatabaseError):
        return False


def load_bound_policy(conn: sqlite3.Connection, fact_id: str) -> LifecyclePolicy:
    row = conn.execute(
        """SELECT p.* FROM memory_fact_lifecycle_policy b
           JOIN memory_lifecycle_policies p
             ON p.policy_version=b.policy_version AND p.target_type=b.target_type
           WHERE b.fact_id=?""", (fact_id,),
    ).fetchone()
    if not row:
        raise PolicyIntegrityError("missing lifecycle policy binding or snapshot")
    try:
        policy = LifecyclePolicy(
            policy_version=row["policy_version"], target_type=row["target_type"],
            default_ttl_seconds=row["default_ttl_seconds"], review_interval_seconds=row["review_interval_seconds"],
            revalidation_window_seconds=row["revalidation_window_seconds"],
            max_revalidation_horizon_seconds=row["max_revalidation_horizon_seconds"],
            expiry_required=bool(row["expiry_required"]), negative_review_threshold=row["negative_review_threshold"],
            high_risk_behavior=row["high_risk_behavior"], effective_from=row["effective_from"],
            effective_to=row["effective_to"],
        )
    except Exception as exc:
        raise PolicyIntegrityError("invalid persisted lifecycle policy") from exc
    if row["policy_hash"] != policy_hash(policy):
        raise PolicyIntegrityError("persisted lifecycle policy hash mismatch")
    return policy
