"""Pending review endpoints (ProposedUpdates with needs_review)."""

import logging
import sqlite3

from fastapi import APIRouter, Depends, HTTPException

from src.api.auth import CurrentActor
from src.api.deps import get_conn, get_current_actor, get_write_conn
from src.api.schemas.common import ActionResult
from src.api.schemas.reviews import ReviewActionRequest, ReviewItemResponse
from src.auth.rbac import P_BUSINESS_WRITE, require_permission
from src.services.review_service import review_approve, review_reject

router = APIRouter(tags=["reviews"])
logger = logging.getLogger(__name__)


@router.get("/reviews", response_model=list[ReviewItemResponse])
def list_pending_reviews(conn: sqlite3.Connection = Depends(get_conn)):
    from config.defaults import ACTIVE_PROJECT_STATUSES

    placeholders = ",".join("?" for _ in ACTIVE_PROJECT_STATUSES)
    rows = conn.execute(
        f"""SELECT pu.*, p.client_name
           FROM proposed_updates pu
           LEFT JOIN projects p ON pu.project_id = p.project_id
           WHERE pu.decision_outcome = 'needs_review'
             AND p.project_status IN ({placeholders})
           ORDER BY pu.created_at DESC""",
        ACTIVE_PROJECT_STATUSES,
    ).fetchall()
    return [ReviewItemResponse(**dict(row)) for row in rows]


@router.post("/reviews/{update_id}/action", response_model=ActionResult)
def review_action(
    update_id: str,
    body: ReviewActionRequest,
    actor: CurrentActor = Depends(get_current_actor),
    conn: sqlite3.Connection = Depends(get_write_conn),
):
    require_permission(actor, P_BUSINESS_WRITE)
    post_commit = []
    if body.action == "approve":
        result = review_approve(
            conn,
            update_id,
            actor=actor.user_id,
            surface="dashboard",
            commit=False,
            post_commit=post_commit,
        )
        _commit_review_action(conn, result.outcome, post_commit)
        if result.success:
            applied = ", ".join(result.applied_fields.keys())
            return ActionResult(success=True, message=f"Approved and applied: {applied}")
        return ActionResult(success=False, message=result.message)

    if body.action == "reject":
        result = review_reject(
            conn,
            update_id,
            reason=body.reject_reason or "",
            tags=body.reject_tags,
            actor=actor.user_id,
            surface="dashboard",
            commit=False,
            post_commit=post_commit,
        )
        _commit_review_action(conn, result.outcome, post_commit)
        return ActionResult(success=result.success, message=result.message)

    raise HTTPException(400, f"Unknown action: {body.action}")


def _commit_review_action(
    conn: sqlite3.Connection, outcome: str, post_commit: list,
) -> None:
    """Finish a review before building a success response, not in yield teardown."""
    if not post_commit and outcome != "refreshed":
        return
    conn.commit()
    for effect in post_commit:
        try:
            effect()
        except Exception:
            logger.exception("Committed review API post-commit effect failed")
