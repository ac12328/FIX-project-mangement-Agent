"""AI feedback endpoints — operator corrections on exact AI outputs."""
from __future__ import annotations

import sqlite3

from fastapi import APIRouter, Depends, HTTPException

from config import defaults
from src.api.auth import CurrentActor
from src.api.deps import get_conn, get_write_actor, get_write_conn
from src.api.schemas.ai_feedback import (
    FeedbackListResponse, FeedbackRequest, FeedbackResponse, FeedbackSummaryResponse,
)
from src.repositories.ai_feedback_repo import (
    get_feedback_summary, insert_feedback, list_feedback_for_project,
)

router = APIRouter(tags=["ai_feedback"])


def _resolve_feedback_linkage(
    conn: sqlite3.Connection, body: FeedbackRequest
) -> dict[str, str | None]:
    """Best-effort trace lookup; explicit request linkage always wins."""
    linkage: dict[str, str | None] = {
        "surface": body.surface,
        "trace_id": body.trace_id,
        "run_id": body.run_id,
        "turn_id": body.turn_id,
        "output_revision_id": body.output_revision_id,
    }
    try:
        if body.trace_id:
            llm_row = conn.execute(
                "SELECT run_id, turn_id FROM llm_traces WHERE trace_id=? LIMIT 1",
                (body.trace_id,),
            ).fetchone()
            if llm_row is not None:
                linkage["run_id"] = linkage["run_id"] or llm_row[0]
                linkage["turn_id"] = linkage["turn_id"] or llm_row[1]

            run_row = conn.execute(
                """SELECT run_id, turn_id, surface
                   FROM trace_runs WHERE trace_id=?
                   ORDER BY started_at DESC LIMIT 1""",
                (body.trace_id,),
            ).fetchone()
            if run_row is not None:
                linkage["run_id"] = linkage["run_id"] or run_row[0]
                linkage["turn_id"] = linkage["turn_id"] or run_row[1]
                linkage["surface"] = linkage["surface"] or run_row[2]

        if linkage["run_id"]:
            run_row = conn.execute(
                "SELECT trace_id, turn_id, surface FROM trace_runs WHERE run_id=? LIMIT 1",
                (linkage["run_id"],),
            ).fetchone()
            if run_row is not None:
                linkage["trace_id"] = linkage["trace_id"] or run_row[0]
                linkage["turn_id"] = linkage["turn_id"] or run_row[1]
                linkage["surface"] = linkage["surface"] or run_row[2]
    except sqlite3.Error:
        # Feedback remains durable even if trace projection is unavailable or behind.
        pass
    return linkage


def _enqueue_feedback_learning(
    conn: sqlite3.Connection, *, body: FeedbackRequest | None = None,
    operator_id: str = "default_operator", item_type: str | None = None,
    item_id: str | None = None, project_id: str | None = None,
    feedback_kind: str | None = None, category: str | None = None,
    operator_note: str | None = None,
) -> tuple[str, str | None, list[str]] | str:
    """Persist feedback/evidence/job and exact attribution atomically."""
    legacy_call = body is None
    if body is None:
        body = FeedbackRequest(
            item_type=item_type or "draft", item_id=item_id or "unknown",
            project_id=project_id, feedback_kind=feedback_kind or "wrong",
            category=category, operator_note=operator_note,
        )
    from src.learning.governance_flags import capture_enabled, outbox_v2_enabled
    from src.learning.ingest.feedback import capture_operator_feedback
    from src.learning.jobs.outbox_repo import enqueue_job

    linkage = _resolve_feedback_linkage(conn, body)
    feedback_id = insert_feedback(
        conn, item_type=body.item_type, item_id=body.item_id,
        project_id=body.project_id, feedback_kind=body.feedback_kind,
        category=body.category, operator_note=body.operator_note,
        surface=linkage["surface"], trace_id=linkage["trace_id"],
        run_id=linkage["run_id"], turn_id=linkage["turn_id"],
        output_revision_id=linkage["output_revision_id"],
    )
    attribution_status = None
    outcome_ids: list[str] = []
    if defaults.EXACT_OUTCOME_ATTRIBUTION_V2:
        exact = all((body.surface, body.trace_id, body.output_revision_id))
        if not exact:
            attribution_status = "unattributable"
        else:
            from src.learning.evaluation.attribution import AttributionRequest, attribute_outcome
            result = attribute_outcome(conn, AttributionRequest(
                idempotency_key=f"feedback:{feedback_id}", operator_id=operator_id,
                surface=body.surface, trace_id=body.trace_id,
                output_revision_id=body.output_revision_id,
                outcome_type={"correct": "positive", "wrong": "negative", "edited": "correction"}[body.feedback_kind],
                outcome_value=1.0 if body.feedback_kind == "correct" else -1.0,
                actor_id=operator_id, reason=body.operator_note, feedback_id=feedback_id,
                fact_id=body.fact_id, fact_version=body.fact_version,
                feedback_scope=body.feedback_scope,
                expected_artifact_type=body.item_type,
                expected_artifact_id=body.item_id,
                expected_project_id=body.project_id,
            ))
            attribution_status, outcome_ids = result.status, result.outcome_ids
    if not capture_enabled():
        return feedback_id if legacy_call else (feedback_id, attribution_status, outcome_ids)
    text = body.operator_note or f"{body.feedback_kind} feedback for {body.item_type} {body.item_id}"
    observation = capture_operator_feedback(
        conn, source_id=feedback_id, operator_id=operator_id, feedback=text,
        project_id=body.project_id, trace_id=linkage["trace_id"],
        output_revision_id=linkage["output_revision_id"],
    )
    if outbox_v2_enabled():
        enqueue_job(
            conn, observation_id=observation.observation_id,
            job_type="feedback_extraction", extractor_version="feedback-v3",
            payload={"feedback_id": feedback_id, "item_type": body.item_type,
                     "item_id": body.item_id, "feedback_kind": body.feedback_kind,
                     "category": body.category},
        )
    return feedback_id if legacy_call else (feedback_id, attribution_status, outcome_ids)


@router.post("/ai-feedback", response_model=FeedbackResponse)
def post_feedback(
    body: FeedbackRequest,
    conn: sqlite3.Connection = Depends(get_write_conn),
    actor: CurrentActor = Depends(get_write_actor),
):
    """Record authenticated operator feedback with fail-closed v2 linkage."""
    if body.operator_id and body.operator_id != actor.operator_id:
        raise HTTPException(403, "operator_id does not match authenticated operator")
    if defaults.EXACT_OUTCOME_ATTRIBUTION_V2 and not body.operator_id:
        raise HTTPException(422, "operator_id is required when exact attribution v2 is enabled")
    feedback_id, status, outcome_ids = _enqueue_feedback_learning(
        conn, body=body, operator_id=actor.operator_id,
    )
    return FeedbackResponse(
        success=True, feedback_id=feedback_id,
        attribution_status=status, outcome_ids=outcome_ids,
    )


@router.get("/ai-feedback/summary", response_model=FeedbackSummaryResponse)
def feedback_summary(days: int = 7, conn: sqlite3.Connection = Depends(get_conn)):
    return FeedbackSummaryResponse(**get_feedback_summary(conn, days=days))


@router.get("/ai-feedback/project/{project_id}", response_model=FeedbackListResponse)
def feedback_for_project(project_id: str, conn: sqlite3.Connection = Depends(get_conn)):
    return FeedbackListResponse(items=list_feedback_for_project(conn, project_id))
