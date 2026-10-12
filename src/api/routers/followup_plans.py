"""Permission-checked project action-plan reads and atomic operator bundles."""
import sqlite3
import logging
from fastapi import APIRouter,Depends,HTTPException
from src.api.auth import CurrentActor
from src.api.deps import get_conn,get_write_conn,get_current_actor,get_write_actor
from src.auth.rbac import require_permission,P_SHARED_READ,P_BUSINESS_WRITE
from src.services.followup_plan import ActionBundle,ActionPlanError,apply_action_bundle,plan_snapshot

router=APIRouter(tags=["followup-plans"])
logger=logging.getLogger(__name__)

@router.get("/projects/{project_id}/action-plan")
def get_plan(project_id:str,conn:sqlite3.Connection=Depends(get_conn),actor:CurrentActor=Depends(get_current_actor)):
    require_permission(actor,P_SHARED_READ)
    try:return plan_snapshot(conn,project_id)
    except KeyError:raise HTTPException(404,"Project not found") from None

@router.post("/projects/{project_id}/action-plan/bundles")
def apply_plan(project_id:str,body:ActionBundle,conn:sqlite3.Connection=Depends(get_write_conn),actor:CurrentActor=Depends(get_write_actor)):
    require_permission(actor,P_BUSINESS_WRITE)
    try:
        result=apply_action_bundle(conn,project_id=project_id,bundle=body,actor_id=actor.user_id,trusted_operator=True)
        conn.commit()
    except KeyError:
        conn.rollback();raise HTTPException(404,"Project or action not found") from None
    except (ActionPlanError,sqlite3.IntegrityError) as exc:
        conn.rollback();raise HTTPException(409,str(exc)) from None
    if result["outcome"]=="applied":
        try:
            from src.events.bus import Event,event_bus
            event_bus.publish(Event(event_type="project_updated",project_id=project_id,surface="dashboard",detail={"action_plan_revision":result["plan_revision"]}))
        except Exception:logger.exception("Post-commit action-plan event failed")
    return result

from pydantic import BaseModel, ConfigDict, Field
from typing import Literal
from src.services.followup_review import list_action_reviews, decide_action_review, ActionReviewError


class ActionReviewDecisionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    decision_id: str = Field(min_length=1, max_length=120)
    expected_revision: int = Field(ge=0)
    decision: Literal["approved", "rejected"]
    reason: str = Field(min_length=1, max_length=2000)
    bundle: ActionBundle | None = None


@router.get("/projects/{project_id}/action-plan/reviews")
def get_action_reviews(project_id: str, status: Literal["pending", "approved", "rejected"] = "pending",
                       conn: sqlite3.Connection = Depends(get_conn), actor: CurrentActor = Depends(get_current_actor)):
    require_permission(actor, P_SHARED_READ)
    if not conn.execute("SELECT 1 FROM projects WHERE project_id=?", (project_id,)).fetchone():
        raise HTTPException(404, "Project not found")
    return list_action_reviews(conn, project_id, status=status)


@router.post("/projects/{project_id}/action-plan/reviews/{review_id}/decisions")
def decide_plan_review(project_id: str, review_id: str, body: ActionReviewDecisionRequest,
                       conn: sqlite3.Connection = Depends(get_write_conn), actor: CurrentActor = Depends(get_write_actor)):
    require_permission(actor, P_BUSINESS_WRITE)
    try:
        result = decide_action_review(conn, project_id=project_id, review_id=review_id, actor_id=actor.user_id,
            expected_revision=body.expected_revision, decision_id=body.decision_id, decision=body.decision,
            reason=body.reason, bundle=body.bundle, trusted_operator=True)
        conn.commit()
    except KeyError:
        conn.rollback(); raise HTTPException(404, "Project action review not found") from None
    except (ActionReviewError, ActionPlanError, sqlite3.IntegrityError) as exc:
        conn.rollback(); raise HTTPException(409, str(exc)) from None
    if result["outcome"] == "applied":
        try:
            from src.events.bus import Event, event_bus
            event_bus.publish(Event(event_type="project_updated", project_id=project_id, surface="dashboard",
                detail={"action_review_id": review_id, "action_review_status": result["status"]}))
        except Exception:
            logger.exception("Post-commit action review event failed")
    return result
