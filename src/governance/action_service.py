"""Action intent creation, approval, validation, and execution state changes."""

from __future__ import annotations

import hashlib
import json
import sqlite3
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

from src.models.action_intent import ActionIntent, RiskClass
from src.repositories.action_intent_repo import (
    get_action_intent,
    insert_action_intent,
    update_action_status,
)


def canonical_payload_hash(payload: dict[str, Any]) -> str:
    canonical = json.dumps(
        payload,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode()
    return hashlib.sha256(canonical).hexdigest()


def create_action_intent(
    conn: sqlite3.Connection,
    *,
    operator_id: str,
    action_type: str,
    risk_class: RiskClass,
    target_type: str,
    target_id: str,
    payload: dict[str, Any],
    created_by_surface: str,
    target_version: str | None = None,
    expires_in_minutes: int = 30,
    idempotency_key: str | None = None,
) -> ActionIntent:
    now = datetime.now(timezone.utc)
    intent = ActionIntent(
        action_id=f"act_{uuid.uuid4().hex[:16]}",
        operator_id=operator_id,
        action_type=action_type,
        risk_class=risk_class,
        target_type=target_type,
        target_id=target_id,
        target_version=target_version,
        payload=payload,
        payload_hash=canonical_payload_hash(payload),
        status="pending",
        created_by_surface=created_by_surface,
        created_at=now.isoformat(),
        expires_at=(now + timedelta(minutes=max(1, expires_in_minutes))).isoformat(),
        idempotency_key=idempotency_key,
    )
    insert_action_intent(conn, intent)
    return intent


def approve_action_intent(
    conn: sqlite3.Connection, action_id: str, *, approved_by: str
) -> ActionIntent:
    from src.governance.four_eyes import FourEyesError, assert_second_person, team_four_eyes_enabled

    intent = _require_status(conn, action_id, {"pending"})
    _ensure_not_expired(conn, intent)
    if team_four_eyes_enabled():
        try:
            assert_second_person(intent.operator_id, approved_by, conn=conn)
        except FourEyesError as exc:
            raise ValueError(exc.message) from exc
    now = datetime.now(timezone.utc).isoformat()
    update_action_status(
        conn,
        action_id,
        "approved",
        approved_by=approved_by,
        approved_at=now,
    )
    return get_action_intent(conn, action_id)  # type: ignore[return-value]


def reject_action_intent(conn: sqlite3.Connection, action_id: str) -> ActionIntent:
    _require_status(conn, action_id, {"pending", "approved"})
    update_action_status(conn, action_id, "rejected")
    return get_action_intent(conn, action_id)  # type: ignore[return-value]


def begin_action_execution(
    conn: sqlite3.Connection,
    action_id: str,
    *,
    current_payload: dict[str, Any],
    current_target_version: str | None = None,
) -> ActionIntent:
    intent = _require_status(conn, action_id, {"approved"})
    _ensure_not_expired(conn, intent)
    if canonical_payload_hash(current_payload) != intent.payload_hash:
        update_action_status(conn, action_id, "invalidated", error="Payload changed after approval")
        raise ValueError("Action approval is stale because the payload changed")
    if intent.target_version is not None and current_target_version != intent.target_version:
        update_action_status(conn, action_id, "invalidated", error="Target version changed after approval")
        raise ValueError("Action approval is stale because the target version changed")
    update_action_status(conn, action_id, "executing")
    return get_action_intent(conn, action_id)  # type: ignore[return-value]


def complete_action_execution(
    conn: sqlite3.Connection, action_id: str, *, result_ref: str | None = None
) -> ActionIntent:
    _require_status(conn, action_id, {"executing"})
    update_action_status(
        conn,
        action_id,
        "executed",
        executed_at=datetime.now(timezone.utc).isoformat(),
        result_ref=result_ref,
    )
    return get_action_intent(conn, action_id)  # type: ignore[return-value]


def fail_action_execution(
    conn: sqlite3.Connection, action_id: str, *, error: str
) -> ActionIntent:
    _require_status(conn, action_id, {"executing"})
    update_action_status(conn, action_id, "failed", error=error)
    return get_action_intent(conn, action_id)  # type: ignore[return-value]


def _require_status(
    conn: sqlite3.Connection, action_id: str, allowed: set[str]
) -> ActionIntent:
    intent = get_action_intent(conn, action_id)
    if intent is None:
        raise ValueError(f"Action intent {action_id} not found")
    if intent.status not in allowed:
        raise ValueError(f"Action intent {action_id} is {intent.status}, expected {sorted(allowed)}")
    return intent


def _ensure_not_expired(conn: sqlite3.Connection, intent: ActionIntent) -> None:
    expires_at = datetime.fromisoformat(intent.expires_at.replace("Z", "+00:00"))
    if expires_at <= datetime.now(timezone.utc):
        update_action_status(conn, intent.action_id, "expired", error="Approval expired")
        raise ValueError(f"Action intent {intent.action_id} has expired")
