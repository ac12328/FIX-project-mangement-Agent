"""Authenticated, confirmed, idempotent emergency revoke operation."""
from __future__ import annotations

import hashlib
import json
from datetime import datetime, timedelta, timezone
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from src.db.connection import scoped_write
from src.learning.authority import SelectorKind, observation_selectors
from src.learning.governance.revoke_service import governed_deactivate
from src.learning.operation_service import create_operation, transition_operation
from src.learning.rollback_service import create_rollback_work

_ALLOWED_ROLES = {"security_admin", "memory_governor", "system_lifecycle"}


class EmergencyRevokeRequest(BaseModel):
    model_config = ConfigDict(frozen=True)
    selector_kind: SelectorKind
    selector_value: str = Field(min_length=1)
    actor_id: str = Field(min_length=1)
    actor_role: str = Field(min_length=1)
    confirmed: bool
    confirmation_token: str | None = None
    reason: str = Field(min_length=1)
    idempotency_key: str = Field(min_length=1)
    at: datetime
    rollback_slo_seconds: int = Field(default=300, gt=0)


class EmergencyRevokeResult(BaseModel):
    model_config = ConfigDict(frozen=True)
    operation_id: str
    revoked: int
    already_inactive: int
    denylist_created: bool
    status: str


def emergency_revoke(request: EmergencyRevokeRequest | None = None, **legacy) -> EmergencyRevokeResult:
    if request is None:
        if "kind" in legacy:
            mapping = {"source": "source_id", "batch": "batch_id"}
            legacy["selector_kind"] = mapping.get(legacy.pop("kind"), legacy.get("selector_kind"))
        missing = [name for name in ("actor_role", "confirmed", "idempotency_key") if name not in legacy]
        if missing:
            raise ValueError("legacy emergency revoke requires explicit " + ", ".join(missing))
        legacy["selector_value"] = legacy.pop("value", legacy.get("selector_value"))
        request = EmergencyRevokeRequest(**legacy)
    at = _utc(request.at)
    if request.actor_role not in _ALLOWED_ROLES:
        raise PermissionError("actor role is not authorized for emergency revoke")
    if not request.confirmed or (request.confirmation_token is not None and not request.confirmation_token.strip()):
        raise ValueError("explicit emergency revoke confirmation is required")
    request_hash = _hash(request.model_dump(mode="json", exclude={"idempotency_key"}))
    operation_id = f"mop_{request_hash[:24]}"
    deadline = at + timedelta(seconds=request.rollback_slo_seconds)
    with scoped_write() as conn:
        existing = conn.execute("SELECT * FROM memory_lifecycle_operations WHERE idempotency_key=?", (request.idempotency_key,)).fetchone()
        if existing:
            if existing["request_hash"] != request_hash:
                raise ValueError("idempotency key reused with a different emergency revoke request")
            result = json.loads(existing["result_json"])
            return EmergencyRevokeResult(operation_id=existing["operation_id"], status=existing["status"], **result)
        create_operation(conn, operation_id=operation_id, operation_type="emergency_revoke",
            idempotency_key=request.idempotency_key, request_hash=request_hash, actor_id=request.actor_id,
            actor_role=request.actor_role, reason=request.reason, effective_at=at,
            slo_seconds=request.rollback_slo_seconds, selector_kind=request.selector_kind,
            selector_value=request.selector_value)
        deny_id = f"mad_{hashlib.sha256(f'{request.selector_kind}|{request.selector_value}'.encode()).hexdigest()[:24]}"
        deny_created = conn.execute(
            """INSERT OR IGNORE INTO memory_authority_denylist
               (deny_id,authority_kind,authority_value,operation_id,actor_id,actor_role,reason,effective_at,created_at)
               VALUES (?,?,?,?,?,?,?,?,?)""",
            (deny_id, request.selector_kind, request.selector_value, operation_id, request.actor_id,
             request.actor_role, request.reason, at.isoformat(), at.isoformat()),
        ).rowcount == 1
        rows = conn.execute(
            """SELECT f.fact_id,o.source_id,o.content_hash,o.raw_payload_json FROM memory_facts f
               JOIN memory_candidates c ON c.candidate_id=f.candidate_id AND c.candidate_version=f.candidate_version
               JOIN learning_observations o ON o.observation_id=c.observation_id
               ORDER BY f.fact_key,f.fact_version"""
        ).fetchall()
        revoked = inactive = 0
        for row in rows:
            if observation_selectors(row).get(request.selector_kind) != request.selector_value:
                continue
            decision, projection = governed_deactivate(conn, fact_id=row["fact_id"], actor_id=request.actor_id,
                reason=request.reason, decided_at=at, decision_type="revoke", operation_id=operation_id)
            if decision is None:
                inactive += 1
                continue
            revoked += 1
            create_rollback_work(conn, operation_id=operation_id, fact_id=row["fact_id"], projection_event_id=projection)
        result = {"revoked": revoked, "already_inactive": inactive, "denylist_created": deny_created}
        status = "pending" if revoked else "completed"
        conn.execute("UPDATE memory_lifecycle_operations SET result_json=? WHERE operation_id=?",
            (json.dumps(result, sort_keys=True), operation_id))
        if status == "completed":
            transition_operation(conn, operation_id=operation_id, status="completed",
                                 reason="selector matched no active facts", at=at, payload=result)
        return EmergencyRevokeResult(operation_id=operation_id, status=status, **result)


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("at must be timezone-aware")
    return value.astimezone(timezone.utc)


def _hash(value) -> str:
    payload = value if isinstance(value, dict) else value
    return hashlib.sha256(json.dumps(payload, sort_keys=True, default=str, separators=(",", ":")).encode()).hexdigest()
