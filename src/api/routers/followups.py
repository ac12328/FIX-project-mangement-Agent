"""Follow-up tracker endpoints."""

import sqlite3
from datetime import datetime, timezone, timedelta

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from src.api.deps import get_conn, get_write_conn
from src.portfolio.followup_tracker import (
    cancel_followup,
    complete_followup,
    get_open_followups,
    get_followup,
    snooze_followup,
)

router = APIRouter(tags=["followups"])


class FollowupActionRequest(BaseModel):
    action: str  # done | snooze | cancel
    snooze_days: int = Field(default=1, ge=1, le=365)
    expected_revision: int | None = Field(default=None, ge=0)


@router.get("/followups")
def list_open_followups(conn: sqlite3.Connection = Depends(get_conn)):
    return get_open_followups(conn)


@router.get("/projects/{project_id}/followups")
def list_project_followups(
    project_id: str,
    conn: sqlite3.Connection = Depends(get_conn),
):
    return get_open_followups(conn, project_id)


@router.post("/followups/{followup_id}/action")
def followup_action(
    followup_id: str,
    body: FollowupActionRequest,
    conn: sqlite3.Connection = Depends(get_write_conn),
):
    """Perform an action on a follow-up: done, snooze, or cancel."""
    if body.expected_revision is not None:
        current = get_followup(conn, followup_id)
        if current is None:
            raise HTTPException(404, "Follow-up not found")
        if current["action_revision"] != body.expected_revision:
            raise HTTPException(409, "Follow-up changed; refresh before applying this action")
    if body.action == "done":
        ok = complete_followup(conn, followup_id, expected_revision=body.expected_revision)
        if not ok:
            raise HTTPException(404, "Follow-up not found or not open")
        current = get_followup(conn, followup_id)
        return {"success": True, "message": "Follow-up marked done.", "action_revision": current["action_revision"] if current else None}

    if body.action == "snooze":
        new_due = (datetime.now(timezone.utc) + timedelta(days=max(1, body.snooze_days))).strftime("%Y-%m-%d")
        ok, reason = snooze_followup(conn, followup_id, new_due, expected_revision=body.expected_revision)
        if not ok:
            msg = ("Max snoozes reached — follow-up escalated."
                   if reason == "max_snoozes"
                   else "Follow-up not found or not open")
            raise HTTPException(422, msg)
        current = get_followup(conn, followup_id)
        return {"success": True, "message": f"Follow-up snoozed until {new_due}.", "action_revision": current["action_revision"] if current else None}

    if body.action == "cancel":
        ok = cancel_followup(conn, followup_id, expected_revision=body.expected_revision)
        if not ok:
            raise HTTPException(404, "Follow-up not found or not open")
        current = get_followup(conn, followup_id)
        return {"success": True, "message": "Follow-up cancelled.", "action_revision": current["action_revision"] if current else None}

    raise HTTPException(400, f"Unknown action: {body.action}")


class BulkFollowupActionRequest(BaseModel):
    followup_ids: list[str]
    expected_revisions: dict[str, int] = Field(default_factory=dict)
    action: str  # "done" | "snooze" | "cancel"
    snooze_days: int = 1


@router.post("/followups/bulk-action")
def bulk_followup_action(
    body: BulkFollowupActionRequest,
    conn: sqlite3.Connection = Depends(get_write_conn),
):
    """Apply an action (done, snooze, or cancel) to multiple follow-ups at once."""
    if body.action not in ("done", "snooze", "cancel"):
        raise HTTPException(400, f"Unknown action: {body.action}")

    results: list[dict] = []

    for followup_id in body.followup_ids:
        expected_revision = body.expected_revisions.get(followup_id)
        if expected_revision is None:
            results.append({
                "followup_id": followup_id,
                "success": False,
                "message": "expected_revision is required; refresh before applying this action",
            })
            continue
        try:
            if body.action == "done":
                ok = complete_followup(conn, followup_id, expected_revision=expected_revision)
                msg = "Follow-up marked done." if ok else "Follow-up not found or not open"
                current = get_followup(conn, followup_id)
                results.append({"followup_id": followup_id, "success": ok, "message": msg,
                                "action_revision": current["action_revision"] if current else None})

            elif body.action == "snooze":
                new_due = (datetime.now(timezone.utc) + timedelta(days=max(1, body.snooze_days))).strftime("%Y-%m-%d")
                ok, reason = snooze_followup(conn, followup_id, new_due, expected_revision=expected_revision)
                if ok:
                    current = get_followup(conn, followup_id)
                    results.append({"followup_id": followup_id, "success": True, "message": f"Follow-up snoozed until {new_due}.",
                                    "action_revision": current["action_revision"] if current else None})
                else:
                    msg = ("Max snoozes reached — follow-up escalated."
                           if reason == "max_snoozes"
                           else "Follow-up not found or not open")
                    current = get_followup(conn, followup_id)
                    results.append({"followup_id": followup_id, "success": False, "message": msg,
                                    "action_revision": current["action_revision"] if current else None})

            elif body.action == "cancel":
                ok = cancel_followup(conn, followup_id, expected_revision=expected_revision)
                msg = "Follow-up cancelled." if ok else "Follow-up not found, not open, or changed"
                current = get_followup(conn, followup_id)
                results.append({"followup_id": followup_id, "success": ok, "message": msg,
                                "action_revision": current["action_revision"] if current else None})

        except Exception as exc:
            results.append({"followup_id": followup_id, "success": False, "message": str(exc)})

    return {"results": results}
