"""Thin Learning Center mutation adapters."""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException

from src.api.auth import CurrentActor
from src.api.deps import get_write_actor
from src.api.schemas.learning_center import (
    ActionResponse, CandidateActionRequest, ConflictActionRequest, FactActionRequest,
    JobActionRequest, KillSwitchRequest,
)
from src.events.bus import Event, event_bus
from src.learning.confirmation import require_confirmation

router = APIRouter(prefix="/learning-center", tags=["learning-center"])


def _error(exc: Exception) -> HTTPException:
    if isinstance(exc, KeyError): return HTTPException(404, "Learning resource not found")
    if isinstance(exc, PermissionError): return HTTPException(403, str(exc))
    return HTTPException(409, str(exc))


def _publish(response: ActionResponse, **detail) -> None:
    event_bus.publish(Event(event_type=response.event_type,
        detail={**response.model_dump(exclude_none=True), **detail}, surface="dashboard"))


def _confirm(
    body,
    *,
    action: str,
    resource_id: str,
    revision: str,
    required: bool,
    resource_version: int | None = None,
    scope: str | None = None,
    risk: str | None = None,
    actor_id: str | None = None,
) -> None:
    if required:
        require_confirmation(
            action=action,
            resource_id=resource_id,
            expected_revision=revision,
            supplied_revision=body.expected_revision,
            supplied_token=body.confirmation_token,
            confirmed=body.confirmed,
            resource_version=resource_version,
            scope=scope,
            risk=risk,
            actor_id=actor_id,
            reason=(body.reason if any(value is not None for value in (resource_version, scope, risk, actor_id)) else None),
        )


@router.post("/candidates/{candidate_id}/{candidate_version}/actions", response_model=ActionResponse)
def candidate_action(candidate_id: str, candidate_version: int, body: CandidateActionRequest,
                     actor: CurrentActor = Depends(get_write_actor)):
    try:
        from src.learning.candidate_control import act, current_revision
        revision, risk, scope, version = current_revision(
            candidate_id, candidate_version, actor_id=actor.operator_id
        )
        required = scope == "global" or risk in {"high", "critical"} or body.action == "merge"
        _confirm(
            body,
            action=body.action,
            resource_id=candidate_id,
            revision=revision,
            required=required,
            resource_version=version,
            scope=scope,
            risk=risk,
            actor_id=actor.operator_id,
        )
        result = act(candidate_id, candidate_version, action=body.action, actor_id=actor.operator_id,
            reason=body.reason, idempotency_key=body.idempotency_key, proposed_value=body.proposed_value,
            scope_type=body.scope_type, scope_id=body.scope_id, suggested_expiry=body.suggested_expiry,
            merge_candidate_id=body.merge_candidate_id, merge_candidate_version=body.merge_candidate_version)
    except Exception as exc: raise _error(exc) from exc
    response = ActionResponse(action=body.action, event_type="learning_candidate_changed",
        candidate_id=result.candidate_id, candidate_version=result.candidate_version,
        decision_id=result.decision_id, fact_id=result.fact_id, status=result.status)
    _publish(response); return response


@router.post("/facts/{fact_id}/actions", response_model=ActionResponse)
def fact_action(fact_id: str, body: FactActionRequest, actor: CurrentActor = Depends(get_write_actor)):
    try:
        from src.learning.fact_control import create_change_candidate, deactivate, revision, supersede
        current = revision(fact_id, actor_id=actor.operator_id)
        expected = body.operation_id if body.action == "rollback" else current
        _confirm(body, action=body.action, resource_id=fact_id, revision=expected or "",
                 required=body.action in {"revoke", "deactivate", "supersede", "change_expiry", "change_scope", "rollback"})
        if body.action in {"deactivate", "revoke"}:
            result = deactivate(fact_id, actor_id=actor.operator_id, reason=body.reason,
                                idempotency_key=body.idempotency_key)
        elif body.action in {"change_expiry", "change_scope"}:
            result = create_change_candidate(fact_id, actor_id=actor.operator_id, reason=body.reason,
                action=body.action, scope_type=body.scope_type, scope_id=body.scope_id,
                suggested_expiry=body.suggested_expiry)
        elif body.action == "supersede":
            if not body.replacement_candidate_id or not body.replacement_candidate_version:
                raise ValueError("supersede requires replacement candidate id and version")
            result = supersede(fact_id, replacement_candidate_id=body.replacement_candidate_id,
                replacement_candidate_version=body.replacement_candidate_version, actor_id=actor.operator_id,
                reason=body.reason, idempotency_key=body.idempotency_key)
        else:
            if not body.operation_id: raise ValueError("rollback requires operation_id")
            from src.learning.rollback_service import retry_rollback_operation
            raw = retry_rollback_operation(operation_id=body.operation_id, fact_id=fact_id,
                actor_id=actor.operator_id, reason=body.reason, idempotency_key=body.idempotency_key)
            response = ActionResponse(action=body.action, event_type="learning_rollback_changed",
                fact_id=fact_id, operation_id=raw["operation_id"], status=raw["status"])
            _publish(response); return response
    except Exception as exc: raise _error(exc) from exc
    response = ActionResponse(action=body.action, event_type="learning_fact_changed", fact_id=fact_id,
        operation_id=result.operation_id, decision_id=result.decision_id,
        candidate_id=result.candidate_id, candidate_version=result.candidate_version, status=result.status)
    _publish(response); return response


@router.post("/conflicts/{conflict_id}/actions", response_model=ActionResponse)
def conflict_action(conflict_id: str, body: ConflictActionRequest,
                    actor: CurrentActor = Depends(get_write_actor)):
    try:
        from src.db.connection import scoped_read
        from src.learning.control_authorization import authorize, resolve_conflict
        with scoped_read() as conn:
            ownership = authorize(resolve_conflict(conn, conflict_id), operator_id=actor.operator_id,
                                  actor_role="operator")
            row = conn.execute("SELECT updated_at,risk_level FROM memory_conflicts WHERE conflict_id=?", (conflict_id,)).fetchone()
        revision = f"{conflict_id}:{row['updated_at']}"
        _confirm(body, action=body.action, resource_id=conflict_id, revision=revision,
                 required=body.action in {"merge", "supersede"} or row["risk_level"] in {"high", "critical"})
        from src.learning.conflict_control import act
        result = act(conflict_id, action=body.action, actor_id=actor.operator_id, actor_role="operator",
                     reason=body.reason, candidate_id=body.candidate_id, candidate_version=body.candidate_version)
    except Exception as exc: raise _error(exc) from exc
    response = ActionResponse(action=body.action, event_type="learning_conflict_changed",
        conflict_id=conflict_id, candidate_id=result.candidate_id,
        candidate_version=result.candidate_version, status=result.status)
    _publish(response); return response


@router.post("/jobs/{job_id}/actions", response_model=ActionResponse)
def job_action(job_id: str, body: JobActionRequest, actor: CurrentActor = Depends(get_write_actor)):
    try:
        from src.db.connection import scoped_read
        from src.learning.control_authorization import authorize, resolve_job
        with scoped_read() as conn:
            authorize(resolve_job(conn, job_id), operator_id=actor.operator_id)
            row = conn.execute("SELECT state,attempts,extractor_version,updated_at FROM learning_outbox WHERE job_id=?", (job_id,)).fetchone()
        revision = f"{job_id}:{row['state']}:{row['attempts']}:{row['extractor_version']}:{row['updated_at']}"
        _confirm(body, action=body.action, resource_id=job_id, revision=revision,
                 required=body.action in {"quarantine", "replay"})
        from src.learning.jobs.control import control_job
        result = control_job(job_id, action=body.action, actor_id=actor.operator_id, reason=body.reason,
            idempotency_key=body.idempotency_key, extractor_version=body.extractor_version)
    except Exception as exc: raise _error(exc) from exc
    response = ActionResponse(action=body.action, event_type="learning_job_changed",
                              job_id=result["job_id"], status=result["status"])
    _publish(response); return response


@router.post("/kill-switches/{name}", response_model=ActionResponse)
def change_kill_switch(name: str, body: KillSwitchRequest,
                       actor: CurrentActor = Depends(get_write_actor)):
    try:
        from src.learning.governance_flags import operational_state
        current = operational_state().get(name)
        if current is None: raise KeyError(name)
        revision = f"{name}:{str(current).lower()}"
        _confirm(body, action="set_kill_switch", resource_id=name, revision=revision, required=True)
        from src.learning.kill_switch_control import set_switch
        result = set_switch(name, body.value, actor_id=actor.operator_id, reason=body.reason,
                            idempotency_key=body.idempotency_key)
    except Exception as exc: raise _error(exc) from exc
    response = ActionResponse(action="set_kill_switch", event_type="learning_kill_switch_changed",
                              status="updated", effective=result["effective"])
    _publish(response, name=name); return response
