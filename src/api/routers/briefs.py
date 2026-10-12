"""Morning brief API endpoint for dashboard home page."""

import sqlite3
from dataclasses import asdict

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from config import defaults
from src.api.auth import CurrentActor
from src.api.deps import get_current_actor, get_read_conn, get_write_actor, get_write_conn
from src.db.connection import scoped_write

router = APIRouter(tags=["briefs"])


@router.get("/briefs/latest")
def get_latest_brief(conn: sqlite3.Connection = Depends(get_read_conn)):
    """Return the most recent morning brief data for the Dashboard home page."""
    from src.portfolio.morning_brief import generate_morning_brief

    brief = generate_morning_brief(conn)
    items = brief.red_items + brief.yellow_items + brief.green_items
    return {
        "projects": [asdict(item) for item in items],
        "total": brief.total_active,
        "generated_at": brief.generated_at,
    }


class BriefFeedbackRequest(BaseModel):
    snapshot_id: str
    item_id: str
    feedback_type: str


@router.get("/assistant/brief")
def get_personal_brief(
    refresh: bool = False,
    actor: CurrentActor = Depends(get_current_actor),
    rconn: sqlite3.Connection = Depends(get_read_conn),
):
    """Return the latest shared personal brief, generating one when needed."""
    if not (
        defaults.PERSONAL_ASSISTANT_ENABLED
        and defaults.PERSONAL_ASSISTANT_PERSONAL_BRIEF_ENABLED
    ):
        raise HTTPException(404, "Personal brief is disabled")
    from src.assistant.brief.builder import (
        build_personal_brief,
        get_latest_personal_brief,
        save_personal_brief_snapshot,
    )

    if not refresh:
        latest = get_latest_personal_brief(rconn, actor.operator_id)
        if latest:
            return latest
    payload = build_personal_brief(rconn, actor.operator_id)
    with scoped_write() as wconn:
        # A concurrent request may have stored a snapshot while we assembled
        # this one. Reuse it unless the caller explicitly requested refresh.
        if not refresh:
            latest = get_latest_personal_brief(wconn, actor.operator_id)
            if latest:
                return latest
        snapshot_id = save_personal_brief_snapshot(wconn, payload, commit=False)
    payload["snapshot_id"] = snapshot_id
    payload["feedback"] = []
    return payload


@router.post("/assistant/brief/feedback")
def submit_personal_brief_feedback(
    body: BriefFeedbackRequest,
    actor: CurrentActor = Depends(get_write_actor),
    conn: sqlite3.Connection = Depends(get_write_conn),
):
    if body.feedback_type not in {"less_like_this", "always_notify", "not_urgent", "snooze"}:
        raise HTTPException(422, "Unknown feedback type")
    from src.assistant.brief.builder import add_brief_feedback

    row = conn.execute(
        """SELECT operator_id FROM personal_brief_snapshots
           WHERE snapshot_id = ?""",
        (body.snapshot_id,),
    ).fetchone()
    if not row or row["operator_id"] != actor.operator_id:
        raise HTTPException(404, "Brief snapshot not found")
    add_brief_feedback(
        conn,
        body.snapshot_id,
        item_id=body.item_id,
        feedback_type=body.feedback_type,
        commit=False,
    )
    return {"success": True}
