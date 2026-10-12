"""Admin-only Dashboard user, session, and security management endpoints."""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel

from src.api.auth import CurrentActor
from src.api.deps import get_current_actor
from src.auth.local import issue_admin_password_reset
from src.auth.mailer import send_auth_action_email
from src.auth.repository import (
    bind_telegram_identity,
    get_user,
    list_security_events,
    list_users,
    record_security_event,
    unbind_telegram_identity,
    update_user_access,
)
from src.auth.sessions import list_sessions, revoke_all_sessions, revoke_session
from src.db.connection import scoped_read, scoped_write

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/admin", tags=["admin-auth"])


class UserAccessUpdate(BaseModel):
    role: str | None = None
    status: str | None = None


class TelegramIdentityBind(BaseModel):
    telegram_user_id: str


@router.get("/users")
def admin_list_users(
    search: str | None = Query(default=None, max_length=200),
    limit: int = Query(default=200, ge=1, le=500),
    actor: CurrentActor = Depends(get_current_actor),
) -> dict:
    with scoped_read() as conn:
        users = list_users(conn, search=search, limit=limit)
    return {"users": users, "count": len(users)}


@router.patch("/users/{user_id}")
def admin_update_user(
    user_id: str,
    body: UserAccessUpdate,
    actor: CurrentActor = Depends(get_current_actor),
) -> dict:
    if body.role is None and body.status is None:
        raise HTTPException(422, "role or status is required")
    try:
        with scoped_write() as conn:
            before = get_user(conn, user_id)
            if not before:
                raise HTTPException(404, "User not found")
            updated = update_user_access(
                conn,
                user_id=user_id,
                role=body.role,
                status=body.status,
            )
            record_security_event(
                conn,
                "admin.user_access_updated",
                severity="warning",
                actor_user_id=actor.user_id,
                subject_user_id=user_id,
                session_id=actor.session_id,
                metadata={
                    "old_role": before["role"],
                    "new_role": updated["role"],
                    "old_status": before["status"],
                    "new_status": updated["status"],
                },
            )
    except KeyError as exc:
        raise HTTPException(404, "User not found") from exc
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc
    return {"user": updated}


@router.put("/users/{user_id}/telegram-identity")
def admin_bind_telegram_identity(
    user_id: str,
    body: TelegramIdentityBind,
    actor: CurrentActor = Depends(get_current_actor),
) -> dict:
    try:
        with scoped_write() as conn:
            if not get_user(conn, user_id):
                raise HTTPException(404, "User not found")
            updated = bind_telegram_identity(
                conn,
                user_id=user_id,
                telegram_user_id=body.telegram_user_id,
            )
            record_security_event(
                conn,
                "admin.telegram_identity_bound",
                severity="warning",
                actor_user_id=actor.user_id,
                subject_user_id=user_id,
                session_id=actor.session_id,
                metadata={"telegram_user_id": updated.get("telegram_user_id")},
            )
    except HTTPException:
        raise
    except KeyError as exc:
        raise HTTPException(404, "User not found") from exc
    except ValueError as exc:
        status = 409 if "already linked" in str(exc) else 422
        raise HTTPException(status, str(exc)) from exc
    return {"user": updated}


@router.delete("/users/{user_id}/telegram-identity")
def admin_unbind_telegram_identity(
    user_id: str,
    actor: CurrentActor = Depends(get_current_actor),
) -> dict:
    try:
        with scoped_write() as conn:
            if not get_user(conn, user_id):
                raise HTTPException(404, "User not found")
            updated = unbind_telegram_identity(conn, user_id=user_id)
            record_security_event(
                conn,
                "admin.telegram_identity_unbound",
                severity="warning",
                actor_user_id=actor.user_id,
                subject_user_id=user_id,
                session_id=actor.session_id,
            )
    except KeyError as exc:
        raise HTTPException(404, "User not found") from exc
    return {"user": updated}


@router.post("/users/{user_id}/password-reset")
def admin_send_password_reset(
    user_id: str,
    request: Request,
    actor: CurrentActor = Depends(get_current_actor),
) -> dict:
    try:
        with scoped_write() as conn:
            delivery = issue_admin_password_reset(
                conn,
                user_id=user_id,
                requested_ip=request.client.host if request.client else "unknown",
            )
            record_security_event(
                conn,
                "admin.password_reset_requested",
                severity="warning",
                actor_user_id=actor.user_id,
                subject_user_id=user_id,
                session_id=actor.session_id,
            )
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc
    try:
        delivered = send_auth_action_email(delivery)
    except Exception:
        logger.exception("Admin password reset email delivery failed")
        delivered = False
    return {
        "accepted": True,
        "delivered": delivered,
        "message": "Password reset email queued.",
    }


@router.get("/sessions/users/{user_id}")
def admin_user_sessions(
    user_id: str,
    include_terminated: bool = Query(False),
    actor: CurrentActor = Depends(get_current_actor),
) -> dict:
    with scoped_read() as conn:
        if not get_user(conn, user_id):
            raise HTTPException(404, "User not found")
        sessions = list_sessions(
            conn,
            user_id=user_id,
            current_session_id=actor.session_id if actor.user_id == user_id else None,
            include_terminated=include_terminated,
        )
    return {"sessions": sessions}


@router.delete("/sessions/{session_id}")
def admin_revoke_session(
    session_id: str,
    actor: CurrentActor = Depends(get_current_actor),
) -> dict:
    with scoped_write() as conn:
        found = revoke_session(
            conn,
            session_id,
            actor_user_id=actor.user_id,
            allow_admin=True,
            reason="admin_revoked",
        )
    if not found:
        raise HTTPException(404, "Session not found")
    return {"revoked": True, "session_id": session_id}


@router.post("/sessions/users/{user_id}/revoke-all")
def admin_revoke_all_sessions(
    user_id: str,
    actor: CurrentActor = Depends(get_current_actor),
) -> dict:
    with scoped_write() as conn:
        if not get_user(conn, user_id):
            raise HTTPException(404, "User not found")
        count = revoke_all_sessions(
            conn,
            user_id=user_id,
            actor_user_id=actor.user_id,
            reason="admin_revoke_all",
        )
    return {"revoked_sessions": count, "user_id": user_id}


@router.get("/security-events")
def admin_security_events(
    limit: int = Query(default=200, ge=1, le=500),
    event_type: str | None = Query(default=None, max_length=200),
    actor: CurrentActor = Depends(get_current_actor),
) -> dict:
    with scoped_read() as conn:
        events = list_security_events(conn, limit=limit, event_type=event_type)
    return {"events": events, "count": len(events)}
