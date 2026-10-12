"""Preflight, delivery, and durable outbox routes for draft workspaces."""

from __future__ import annotations

import json
import sqlite3

from fastapi import APIRouter, Depends

from src.api.auth import CurrentActor
from src.api.deps import get_current_actor, get_read_conn, get_write_actor, get_write_scope
from src.api.routers.draft_workspace_common import (
    ensure_outbox_draft,
    initialize_legacy_workspace,
    publish_workspace_event,
    raise_workspace_api_error,
)
from src.api.schemas.draft_workspace import (
    RescheduleRequest,
    RetryRequest,
    SendLaterRequest,
    SendNowRequest,
)
from src.email.draft_workspace import public_outbox, public_preflight, public_preview
from src.email.outbox import (
    cancel_scheduled,
    queue_send_later,
    queue_send_now,
    reschedule_outbox,
    retry_outbox,
    undo_send,
)
from src.email.send_gate import SendGateService

router = APIRouter()


@router.get("/drafts/{draft_id}/workspace/preflight")
def workspace_preflight(
    draft_id: str,
    actor: CurrentActor = Depends(get_current_actor),
    conn: sqlite3.Connection = Depends(get_read_conn),
    write_scope=Depends(get_write_scope),
):
    del actor
    try:
        initialize_legacy_workspace(draft_id, conn, write_scope)
        return public_preflight(SendGateService(conn).preflight(draft_id))
    except Exception as exc:
        raise_workspace_api_error(exc)


@router.get("/drafts/{draft_id}/workspace/preview")
def workspace_preview(
    draft_id: str,
    actor: CurrentActor = Depends(get_current_actor),
    conn: sqlite3.Connection = Depends(get_read_conn),
    write_scope=Depends(get_write_scope),
):
    del actor
    try:
        initialize_legacy_workspace(draft_id, conn, write_scope)
        return public_preview(SendGateService(conn).final_preview(draft_id))
    except Exception as exc:
        raise_workspace_api_error(exc)


@router.post("/drafts/{draft_id}/workspace/send-now")
def send_now(
    draft_id: str,
    body: SendNowRequest,
    actor: CurrentActor = Depends(get_write_actor),
    write_scope=Depends(get_write_scope),
):
    try:
        with write_scope() as conn:
            outbox = queue_send_now(
                conn, draft_id, actor_user_id=actor.user_id,
                expected_revision=body.expected_revision,
                expected_hash=body.expected_hash,
                confirmed_warnings=body.confirmed_warnings,
                commit=False,
            )
            response = public_outbox(outbox)
    except Exception as exc:
        raise_workspace_api_error(exc)
    publish_workspace_event(draft_id, "draft_send_queued", {"outbox_id": outbox["outbox_id"]})
    return response


@router.post("/drafts/{draft_id}/workspace/send-later")
def send_later(
    draft_id: str,
    body: SendLaterRequest,
    actor: CurrentActor = Depends(get_write_actor),
    write_scope=Depends(get_write_scope),
):
    try:
        with write_scope() as conn:
            outbox = queue_send_later(
                conn, draft_id, actor_user_id=actor.user_id,
                expected_revision=body.expected_revision,
                expected_hash=body.expected_hash,
                confirmed_warnings=body.confirmed_warnings,
                scheduled_for=body.scheduled_for,
                commit=False,
            )
            response = public_outbox(outbox)
    except Exception as exc:
        raise_workspace_api_error(exc)
    publish_workspace_event(
        draft_id, "draft_send_scheduled",
        {"outbox_id": outbox["outbox_id"], "scheduled_for": outbox.get("scheduled_for")},
    )
    return response


@router.post("/drafts/{draft_id}/workspace/outbox/{outbox_id}/undo")
def outbox_undo(
    draft_id: str,
    outbox_id: str,
    actor: CurrentActor = Depends(get_write_actor),
    write_scope=Depends(get_write_scope),
):
    try:
        with write_scope() as conn:
            ensure_outbox_draft(conn, draft_id, outbox_id)
            result = undo_send(conn, outbox_id, actor_user_id=actor.user_id, commit=False)
            response = public_outbox(result)
    except Exception as exc:
        raise_workspace_api_error(exc)
    publish_workspace_event(draft_id, "draft_send_undone", {"outbox_id": outbox_id})
    return response


@router.post("/drafts/{draft_id}/workspace/outbox/{outbox_id}/cancel")
def outbox_cancel(
    draft_id: str,
    outbox_id: str,
    actor: CurrentActor = Depends(get_write_actor),
    write_scope=Depends(get_write_scope),
):
    try:
        with write_scope() as conn:
            ensure_outbox_draft(conn, draft_id, outbox_id)
            result = cancel_scheduled(conn, outbox_id, actor_user_id=actor.user_id, commit=False)
            response = public_outbox(result)
    except Exception as exc:
        raise_workspace_api_error(exc)
    publish_workspace_event(draft_id, "draft_schedule_cancelled", {"outbox_id": outbox_id})
    return response


@router.post("/drafts/{draft_id}/workspace/outbox/{outbox_id}/reschedule")
def outbox_reschedule(
    draft_id: str,
    outbox_id: str,
    body: RescheduleRequest,
    actor: CurrentActor = Depends(get_write_actor),
    write_scope=Depends(get_write_scope),
):
    try:
        with write_scope() as conn:
            ensure_outbox_draft(conn, draft_id, outbox_id)
            result = reschedule_outbox(
                conn, outbox_id, actor_user_id=actor.user_id,
                scheduled_for=body.scheduled_for, commit=False,
            )
            response = public_outbox(result)
    except Exception as exc:
        raise_workspace_api_error(exc)
    publish_workspace_event(
        draft_id, "draft_schedule_rescheduled",
        {"outbox_id": outbox_id, "scheduled_for": result.get("scheduled_for")},
    )
    return response


@router.get("/drafts/{draft_id}/workspace/outbox/{outbox_id}/preview")
def outbox_preview(
    draft_id: str,
    outbox_id: str,
    actor: CurrentActor = Depends(get_current_actor),
    conn: sqlite3.Connection = Depends(get_read_conn),
):
    del actor
    try:
        ensure_outbox_draft(conn, draft_id, outbox_id)
        row = conn.execute(
            "SELECT snapshot_json FROM draft_outbox WHERE outbox_id=?",
            (outbox_id,),
        ).fetchone()
        if not row:
            raise ValueError("Outbox record not found")
        snapshot = json.loads(row["snapshot_json"])
        return public_preflight(SendGateService(conn).preflight(snapshot))
    except Exception as exc:
        raise_workspace_api_error(exc)


@router.post("/drafts/{draft_id}/workspace/outbox/{outbox_id}/retry")
def outbox_retry(
    draft_id: str,
    outbox_id: str,
    body: RetryRequest,
    actor: CurrentActor = Depends(get_write_actor),
    write_scope=Depends(get_write_scope),
):
    try:
        with write_scope() as conn:
            ensure_outbox_draft(conn, draft_id, outbox_id)
            result = retry_outbox(
                conn, outbox_id, actor_user_id=actor.user_id,
                confirmed_warnings=body.confirmed_warnings, commit=False,
            )
            response = public_outbox(result)
    except Exception as exc:
        raise_workspace_api_error(exc)
    publish_workspace_event(draft_id, "draft_send_retry_queued", {"outbox_id": outbox_id})
    return response
