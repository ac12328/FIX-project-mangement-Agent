"""Secure draft workspace API endpoints."""

from __future__ import annotations

import sqlite3
from fastapi import APIRouter, Depends

from config.settings import CONFIG
from src.api.auth import CurrentActor
from src.api.deps import get_current_actor, get_read_conn, get_write_actor, get_write_scope
from src.api.schemas.draft_workspace import (
    AIProposalApplyRequest,
    AIProposalCreateRequest,
    CheckpointCreateRequest,
    ConfirmReplyRequest,
    RevisionRequest,
    WorkspacePatchRequest,
)
from src.api.routers.draft_workspace_common import (
    initialize_legacy_workspace,
    publish_workspace_event,
    raise_workspace_api_error,
    require_workspace_enabled,
)
from src.api.routers.draft_workspace_collaboration import router as collaboration_router
from src.api.routers.draft_workspace_outbox import router as outbox_router
from src.email.ai_proposals import (
    StaleAIProposalError,
    apply_ai_proposal,
    create_ai_proposal,
    reject_ai_proposal,
)
from src.email.draft_workspace import (
    build_workspace_snapshot,
    compare_checkpoint,
    create_checkpoint,
    get_workspace,
    patch_workspace,
    public_workspace_snapshot,
    restore_checkpoint,
)
from src.email.recipients import configured_self_identities, safe_reply_all
from src.email.sending import email_config_dict

router = APIRouter(
    tags=["draft-workspace"],
    dependencies=[Depends(require_workspace_enabled)],
)


@router.get("/drafts/{draft_id}/workspace")
def workspace_detail(
    draft_id: str,
    actor: CurrentActor = Depends(get_current_actor),
    conn: sqlite3.Connection = Depends(get_read_conn),
    write_scope=Depends(get_write_scope),
):
    del actor
    try:
        initialize_legacy_workspace(draft_id, conn, write_scope)
        return get_workspace(conn, draft_id)
    except Exception as exc:
        raise_workspace_api_error(exc)


@router.patch("/drafts/{draft_id}/workspace")
def workspace_patch(
    draft_id: str,
    body: WorkspacePatchRequest,
    actor: CurrentActor = Depends(get_write_actor),
    write_scope=Depends(get_write_scope),
):
    try:
        with write_scope() as conn:
            result = patch_workspace(
                conn, draft_id, body.patch,
                expected_revision=body.expected_revision,
                actor_user_id=actor.user_id, lease_token=body.lease_token,
                commit=False,
            )
    except Exception as exc:
        raise_workspace_api_error(exc)
    publish_workspace_event(
        draft_id, "draft_workspace_updated",
        {"revision": result["draft"]["revision"]["number"]},
    )
    return result


@router.post("/drafts/{draft_id}/workspace/delivery/new")
def confirm_new_email(
    draft_id: str,
    body: RevisionRequest,
    actor: CurrentActor = Depends(get_write_actor),
    write_scope=Depends(get_write_scope),
):
    try:
        with write_scope() as conn:
            result = patch_workspace(
                conn, draft_id,
                {"delivery_context": {"delivery_mode": "new", "reply_to_email_id": None}},
                expected_revision=body.expected_revision,
                actor_user_id=actor.user_id, lease_token=body.lease_token,
                commit=False,
            )
    except Exception as exc:
        raise_workspace_api_error(exc)
    publish_workspace_event(draft_id, "draft_delivery_context_confirmed", {"delivery_mode": "new"})
    return result


@router.post("/drafts/{draft_id}/workspace/delivery/reply")
def confirm_reply_thread(
    draft_id: str,
    body: ConfirmReplyRequest,
    actor: CurrentActor = Depends(get_write_actor),
    write_scope=Depends(get_write_scope),
):
    try:
        with write_scope() as conn:
            snapshot = build_workspace_snapshot(conn, draft_id, commit_initialization=False)
            anchor = conn.execute(
                "SELECT project_id FROM processed_emails WHERE email_id=?",
                (body.email_id,),
            ).fetchone()
            if not anchor:
                raise ValueError("Reply email not found")
            if snapshot.get("project_id") and anchor["project_id"] != snapshot.get("project_id"):
                raise ValueError("Reply email does not belong to this draft project")
            recipients = safe_reply_all(
                conn, body.email_id,
                self_identities=configured_self_identities(email_config_dict(CONFIG)),
            )
            result = patch_workspace(
                conn, draft_id,
                {
                    "delivery_context": {"delivery_mode": "reply", "reply_to_email_id": body.email_id},
                    "envelope": recipients,
                },
                expected_revision=body.expected_revision,
                actor_user_id=actor.user_id, lease_token=body.lease_token,
                commit=False,
            )
    except Exception as exc:
        raise_workspace_api_error(exc)
    publish_workspace_event(
        draft_id, "draft_delivery_context_confirmed",
        {"delivery_mode": "reply", "email_id": body.email_id},
    )
    return result


@router.post("/drafts/{draft_id}/workspace/checkpoints", status_code=201)
def checkpoint_create(
    draft_id: str,
    body: CheckpointCreateRequest,
    actor: CurrentActor = Depends(get_write_actor),
    write_scope=Depends(get_write_scope),
):
    try:
        with write_scope() as conn:
            result = create_checkpoint(
                conn, draft_id, actor_user_id=actor.user_id,
                reason=body.reason, commit=False,
            )
    except Exception as exc:
        raise_workspace_api_error(exc)
    publish_workspace_event(draft_id, "draft_checkpoint_created", {"version": result["version"]})
    return result


@router.get("/drafts/{draft_id}/workspace/checkpoints/{version}/compare")
def checkpoint_compare(
    draft_id: str,
    version: int,
    actor: CurrentActor = Depends(get_current_actor),
    conn: sqlite3.Connection = Depends(get_read_conn),
    write_scope=Depends(get_write_scope),
):
    del actor
    try:
        initialize_legacy_workspace(draft_id, conn, write_scope)
        comparison = compare_checkpoint(conn, draft_id, version)
        return {
            **comparison,
            "snapshot_before": public_workspace_snapshot(comparison["snapshot_before"]),
            "snapshot_after": public_workspace_snapshot(comparison["snapshot_after"]),
        }
    except Exception as exc:
        raise_workspace_api_error(exc)


@router.post("/drafts/{draft_id}/workspace/checkpoints/{version}/restore")
def checkpoint_restore(
    draft_id: str,
    version: int,
    body: RevisionRequest,
    actor: CurrentActor = Depends(get_write_actor),
    write_scope=Depends(get_write_scope),
):
    try:
        with write_scope() as conn:
            result = restore_checkpoint(
                conn, draft_id, version,
                expected_revision=body.expected_revision,
                actor_user_id=actor.user_id, lease_token=body.lease_token,
                commit=False,
            )
    except Exception as exc:
        raise_workspace_api_error(exc)
    publish_workspace_event(draft_id, "draft_checkpoint_restored", {"version": version})
    return result


router.include_router(collaboration_router)


@router.post("/drafts/{draft_id}/workspace/ai-proposals", status_code=201)
def ai_proposal_create(
    draft_id: str,
    body: AIProposalCreateRequest,
    actor: CurrentActor = Depends(get_write_actor),
    write_scope=Depends(get_write_scope),
):
    try:
        with write_scope() as conn:
            result = create_ai_proposal(
                conn, draft_id, actor_user_id=actor.user_id,
                **body.model_dump(), commit=False,
            )
    except Exception as exc:
        raise_workspace_api_error(exc)
    publish_workspace_event(draft_id, "draft_ai_proposal_created", {"proposal_id": result["proposal_id"]})
    return result


@router.post("/drafts/{draft_id}/workspace/ai-proposals/{proposal_id}/apply")
def ai_proposal_apply(
    draft_id: str,
    proposal_id: str,
    body: AIProposalApplyRequest,
    actor: CurrentActor = Depends(get_write_actor),
    write_scope=Depends(get_write_scope),
):
    stale_error = None
    try:
        with write_scope() as conn:
            proposal = conn.execute(
                "SELECT draft_id FROM draft_ai_proposals WHERE proposal_id=?",
                (proposal_id,),
            ).fetchone()
            if not proposal or proposal["draft_id"] != draft_id:
                raise ValueError("AI proposal not found")
            try:
                result = apply_ai_proposal(
                    conn, proposal_id, actor_user_id=actor.user_id,
                    selected_changes=body.selected_changes,
                    lease_token=body.lease_token, commit=False,
                )
            except StaleAIProposalError as exc:
                # A stale proposal is a durable state transition, not a successful apply.
                stale_error = exc
    except Exception as exc:
        raise_workspace_api_error(exc)
    if stale_error is not None:
        raise_workspace_api_error(stale_error)
    publish_workspace_event(draft_id, "draft_ai_proposal_applied", {"proposal_id": proposal_id})
    return result


@router.post("/drafts/{draft_id}/workspace/ai-proposals/{proposal_id}/reject")
def ai_proposal_reject(
    draft_id: str,
    proposal_id: str,
    actor: CurrentActor = Depends(get_write_actor),
    write_scope=Depends(get_write_scope),
):
    del actor
    try:
        with write_scope() as conn:
            proposal = conn.execute(
                "SELECT draft_id FROM draft_ai_proposals WHERE proposal_id=?",
                (proposal_id,),
            ).fetchone()
            if not proposal or proposal["draft_id"] != draft_id:
                raise ValueError("AI proposal not found")
            result = reject_ai_proposal(conn, proposal_id, commit=False)
    except Exception as exc:
        raise_workspace_api_error(exc)
    publish_workspace_event(draft_id, "draft_ai_proposal_rejected", {"proposal_id": proposal_id})
    return result


router.include_router(outbox_router)
