"""Read and reconcile committed-update delivery without replaying project writes."""
from __future__ import annotations
import sqlite3
import logging
from typing import Literal
from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, ConfigDict, Field
from src.api.auth import CurrentActor
from src.api.deps import get_conn, get_current_actor, get_write_actor, get_write_conn
from src.auth.rbac import P_ADMIN_SECURITY, P_BUSINESS_WRITE, P_SHARED_READ, require_permission
from src.email.update_notification_outbox import (
    NotificationReviewError, notification_status, reconcile_update_notification,
)

logger = logging.getLogger(__name__)
router = APIRouter(tags=["update-notifications"])

class NotificationStatusResponse(BaseModel):
    update_id: str
    project_id: str
    notification_kind: Literal["update_undo", "pct_completed", "review_card"] = "update_undo"
    state: Literal["pending", "sending", "sent", "uncertain", "cancelled"]
    attempts: int
    available_at: str
    telegram_message_id: str | None
    last_error: str | None
    updated_at: str

class NotificationReviewRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    decision: Literal["confirm_sent", "confirm_not_sent", "cancel", "bind_current_destination"]
    expected_updated_at: str = Field(min_length=1, max_length=100)
    reason: str = Field(min_length=1, max_length=2000)
    telegram_message_id: int | None = Field(default=None, gt=0, strict=True)

@router.get("/projects/{project_id}/update-notifications", response_model=list[NotificationStatusResponse])
def list_update_notifications(project_id: str, limit: int = Query(default=50, ge=1, le=100),
                              actor: CurrentActor = Depends(get_current_actor),
                              conn: sqlite3.Connection = Depends(get_conn)):
    require_permission(actor, P_SHARED_READ)
    if not conn.execute("SELECT 1 FROM projects WHERE project_id=?", (project_id,)).fetchone():
        raise HTTPException(404, "Project not found")
    rows = conn.execute(
        "SELECT update_id,project_id,state,attempts,available_at,telegram_message_id,"
        "last_error,updated_at,COALESCE(json_extract(payload_json, '$._notification_kind'),'update_undo') AS notification_kind FROM email_update_notification_outbox WHERE project_id=? "
        "ORDER BY updated_at DESC,update_id LIMIT ?", (project_id, limit),
    ).fetchall()
    return [dict(row) for row in rows]


@router.get("/projects/{project_id}/updates/{update_id}/notification", response_model=NotificationStatusResponse)
def get_update_notification(project_id: str, update_id: str,
                            actor: CurrentActor = Depends(get_current_actor),
                            conn: sqlite3.Connection = Depends(get_conn)):
    require_permission(actor, P_SHARED_READ)
    result = notification_status(conn, project_id=project_id, update_id=update_id)
    if result is None: raise HTTPException(404, "Notification not found")
    return result

@router.post("/projects/{project_id}/updates/{update_id}/notification/reconcile", response_model=NotificationStatusResponse)
def reconcile_notification(project_id: str, update_id: str, body: NotificationReviewRequest,
                           actor: CurrentActor = Depends(get_write_actor),
                           conn: sqlite3.Connection = Depends(get_write_conn)):
    require_permission(actor, P_BUSINESS_WRITE)
    if body.decision == "bind_current_destination":
        require_permission(actor, P_ADMIN_SECURITY)
    try:
        result = reconcile_update_notification(conn, project_id=project_id, update_id=update_id,
            actor_id=actor.user_id, expected_updated_at=body.expected_updated_at,
            decision=body.decision, reason=body.reason,
            telegram_message_id=body.telegram_message_id)
        conn.commit()
        try:
            from src.events.bus import Event, event_bus
            event_bus.publish(Event(event_type="email_notification_updated", project_id=project_id,
                surface="dashboard", detail={"update_id": update_id, "state": result["state"]}))
        except Exception:
            logger.exception("Post-commit notification review event failed for %s", update_id)
        return result
    except KeyError:
        raise HTTPException(404, "Notification not found") from None
    except NotificationReviewError as exc:
        raise HTTPException(409, str(exc)) from None
