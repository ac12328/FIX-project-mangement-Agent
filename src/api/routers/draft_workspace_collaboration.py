"""Review, lease, and comment routes for draft workspaces."""

from __future__ import annotations

import sqlite3

from fastapi import APIRouter, Depends

from config.settings import CONFIG
from src.api.auth import CurrentActor
from src.api.deps import get_write_actor, get_write_scope
from src.api.routers.draft_workspace_common import publish_workspace_event, raise_workspace_api_error
from src.api.schemas.draft_workspace import (
    CommentCreateRequest,
    CommentResolveRequest,
    LeaseTokenRequest,
    ReviewRequest,
)
from src.email.draft_workspace import public_workspace_snapshot
from src.email.workspace_collaboration import (
    acquire_lease,
    approve_review,
    cancel_review,
    create_comment,
    release_lease,
    renew_lease,
    request_changes,
    request_review,
    resolve_comment,
    take_over_lease,
)

router = APIRouter()


def _validate_reviewer(conn: sqlite3.Connection, reviewer_user_id: str) -> None:
    table = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='dashboard_users'"
    ).fetchone()
    if not table or not CONFIG.dashboard_auth.enabled:
        return
    row = conn.execute(
        "SELECT role, status FROM dashboard_users WHERE user_id=?",
        (reviewer_user_id,),
    ).fetchone()
    if not row or row["status"] != "active" or row["role"] not in {"Editor", "Admin"}:
        raise ValueError("Reviewer must be an active user with review permission")


@router.post("/drafts/{draft_id}/workspace/lease/acquire")
def lease_acquire(
    draft_id: str,
    actor: CurrentActor = Depends(get_write_actor),
    write_scope=Depends(get_write_scope),
):
    try:
        with write_scope() as conn:
            result = acquire_lease(
                conn, draft_id, actor_user_id=actor.user_id,
                actor_display_name=actor.display_name, commit=False,
            )
    except Exception as exc:
        raise_workspace_api_error(exc)
    publish_workspace_event(draft_id, "draft_edit_lease_acquired")
    return result


@router.post("/drafts/{draft_id}/workspace/lease/renew")
def lease_renew(
    draft_id: str,
    body: LeaseTokenRequest,
    actor: CurrentActor = Depends(get_write_actor),
    write_scope=Depends(get_write_scope),
):
    try:
        with write_scope() as conn:
            result = renew_lease(
                conn, draft_id, actor_user_id=actor.user_id,
                lease_token=body.lease_token, commit=False,
            )
    except Exception as exc:
        raise_workspace_api_error(exc)
    return result


@router.post("/drafts/{draft_id}/workspace/lease/release")
def lease_release(
    draft_id: str,
    body: LeaseTokenRequest,
    actor: CurrentActor = Depends(get_write_actor),
    write_scope=Depends(get_write_scope),
):
    try:
        with write_scope() as conn:
            released = release_lease(
                conn, draft_id, actor_user_id=actor.user_id,
                lease_token=body.lease_token, commit=False,
            )
    except Exception as exc:
        raise_workspace_api_error(exc)
    if released:
        publish_workspace_event(draft_id, "draft_edit_lease_released")
    return {"released": released}


@router.post("/drafts/{draft_id}/workspace/lease/takeover")
def lease_takeover(
    draft_id: str,
    actor: CurrentActor = Depends(get_write_actor),
    write_scope=Depends(get_write_scope),
):
    try:
        with write_scope() as conn:
            lease = take_over_lease(
                conn, draft_id, actor_user_id=actor.user_id,
                actor_display_name=actor.display_name, commit=False,
            )
    except Exception as exc:
        raise_workspace_api_error(exc)
    publish_workspace_event(draft_id, "draft_edit_lease_taken_over")
    return lease


@router.post("/drafts/{draft_id}/workspace/review/request")
def review_request(
    draft_id: str,
    body: ReviewRequest,
    actor: CurrentActor = Depends(get_write_actor),
    write_scope=Depends(get_write_scope),
):
    try:
        with write_scope() as conn:
            _validate_reviewer(conn, body.reviewer_user_id)
            result = request_review(
                conn, draft_id, requester_user_id=actor.user_id,
                reviewer_user_id=body.reviewer_user_id, commit=False,
            )
            response = public_workspace_snapshot(result)
    except Exception as exc:
        raise_workspace_api_error(exc)
    publish_workspace_event(
        draft_id, "draft_review_requested", {"reviewer_user_id": body.reviewer_user_id},
    )
    return response


@router.post("/drafts/{draft_id}/workspace/review/approve")
def review_approve(
    draft_id: str,
    actor: CurrentActor = Depends(get_write_actor),
    write_scope=Depends(get_write_scope),
):
    try:
        with write_scope() as conn:
            result = approve_review(conn, draft_id, reviewer_user_id=actor.user_id, commit=False)
            response = public_workspace_snapshot(result)
    except Exception as exc:
        raise_workspace_api_error(exc)
    publish_workspace_event(draft_id, "draft_review_approved")
    return response


@router.post("/drafts/{draft_id}/workspace/review/changes")
def review_changes(
    draft_id: str,
    actor: CurrentActor = Depends(get_write_actor),
    write_scope=Depends(get_write_scope),
):
    try:
        with write_scope() as conn:
            result = request_changes(conn, draft_id, reviewer_user_id=actor.user_id, commit=False)
            response = public_workspace_snapshot(result)
    except Exception as exc:
        raise_workspace_api_error(exc)
    publish_workspace_event(draft_id, "draft_review_changes_requested")
    return response


@router.post("/drafts/{draft_id}/workspace/review/cancel")
def review_cancel(
    draft_id: str,
    actor: CurrentActor = Depends(get_write_actor),
    write_scope=Depends(get_write_scope),
):
    try:
        with write_scope() as conn:
            result = cancel_review(conn, draft_id, actor_user_id=actor.user_id, commit=False)
            response = public_workspace_snapshot(result)
    except Exception as exc:
        raise_workspace_api_error(exc)
    publish_workspace_event(draft_id, "draft_review_cancelled")
    return response


@router.post("/drafts/{draft_id}/workspace/comments", status_code=201)
def comment_create(
    draft_id: str,
    body: CommentCreateRequest,
    actor: CurrentActor = Depends(get_write_actor),
    write_scope=Depends(get_write_scope),
):
    try:
        with write_scope() as conn:
            comment = create_comment(
                conn, draft_id, author_user_id=actor.user_id,
                author_display_name=actor.display_name, **body.model_dump(), commit=False,
            )
    except Exception as exc:
        raise_workspace_api_error(exc)
    publish_workspace_event(draft_id, "draft_comment_created", {"comment_id": comment["comment_id"]})
    return comment


@router.patch("/drafts/{draft_id}/workspace/comments/{comment_id}")
def comment_resolve(
    draft_id: str,
    comment_id: str,
    body: CommentResolveRequest,
    actor: CurrentActor = Depends(get_write_actor),
    write_scope=Depends(get_write_scope),
):
    try:
        with write_scope() as conn:
            result = resolve_comment(
                conn, draft_id, comment_id, actor_user_id=actor.user_id,
                resolved=body.resolved, commit=False,
            )
    except Exception as exc:
        raise_workspace_api_error(exc)
    publish_workspace_event(
        draft_id, "draft_comment_resolved" if body.resolved else "draft_comment_reopened",
        {"comment_id": comment_id},
    )
    return result


