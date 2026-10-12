"""Project list, detail, and status endpoints."""

import logging
import sqlite3
from uuid import uuid4

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import JSONResponse

from src.api.auth import CurrentActor
from src.api.deps import get_conn, get_current_actor, get_write_conn
from src.api.schemas.chat import (
    ChatHistoryResponse,
    ChatSendResponse,
    build_ephemeral_turn,
    chat_capabilities,
    normalize_session_messages,
)
from pydantic import BaseModel

from src.auth.rbac import (
    P_BUSINESS_WRITE,
    ROLE_ADMIN,
    ROLE_EDITOR,
    is_read_only_actor,
    require_permission,
)
from src.api.schemas.projects import (
    ProjectSummaryResponse,
    ProjectDetailResponse,
    ProjectStatusResponse,
    ProjectNotesUpdate,
    ProjectSummaryAI,
    ProjectDraftSuggestion,
    ProjectChatRequest,
)
from src.events.bus import Event, event_bus
from src.llm.client import llm_call
from src.models.enums import MILESTONE_DISPLAY_NAMES
from src.repositories.activity_log_repo import insert_activity
from src.repositories.project_repo import get_all_projects, get_active_projects, get_project, update_project_fields
from src.status.answer import answer_status

router = APIRouter(tags=["projects"])
logger = logging.getLogger(__name__)


class ProjectOwnerUpdate(BaseModel):
    owner_user_id: str


@router.get("/projects", response_model=list[ProjectSummaryResponse])
def list_projects(
    status: str | None = Query(None),
    ball_with: str | None = Query(None),
    milestone: str | None = Query(None),
    owner: str | None = Query(None),
    mine: bool = Query(False),
    active_only: bool = Query(False),
    actor: CurrentActor = Depends(get_current_actor),
    conn: sqlite3.Connection = Depends(get_conn),
):
    try:
        projects = get_active_projects(conn) if active_only else get_all_projects(conn)

        # Exclude internal system pseudo-projects (used for working_memory FK anchoring)
        projects = [p for p in projects if not p.project_id.startswith("_")]

        owner_filter = actor.user_id if mine else owner
        if owner_filter:
            projects = [p for p in projects if p.operator_id == owner_filter]
        if status:
            projects = [p for p in projects if p.project_status.value == status]
        if ball_with:
            projects = [p for p in projects if p.ball_with.value == ball_with]
        if milestone:
            projects = [
                p for p in projects
                if p.current_milestone and p.current_milestone.value == milestone
            ]

        return [_to_summary(p) for p in projects]
    except Exception:
        logger.exception("Failed to list projects")
        return JSONResponse(
            status_code=500,
            content={"error": True, "message": "Could not load projects."},
        )


@router.get("/projects/{project_id}", response_model=ProjectDetailResponse)
def get_project_detail(
    project_id: str,
    conn: sqlite3.Connection = Depends(get_conn),
):
    if project_id.startswith("_"):
        raise HTTPException(404, f"Project {project_id} not found")
    project = get_project(conn, project_id)
    if not project:
        raise HTTPException(404, f"Project {project_id} not found")
    return _to_detail(project)


@router.get("/projects/{project_id}/status", response_model=ProjectStatusResponse)
def get_project_status(
    project_id: str,
    question: str | None = Query(None),
    conn: sqlite3.Connection = Depends(get_conn),
):
    status = answer_status(
        conn,
        project_id,
        question=question,
        llm_fn=llm_call if question else None,
    )
    if status is None:
        raise HTTPException(404, f"Project '{project_id}' not found")
    return _to_status_response(status)


def _to_summary(p) -> ProjectSummaryResponse:
    ms_val = p.current_milestone.value if p.current_milestone else None
    return ProjectSummaryResponse(
        project_id=p.project_id,
        client_name=p.client_name,
        client_email=p.client_email,
        market=p.market,
        project_status=p.project_status.value,
        ball_with=p.ball_with.value,
        current_milestone=ms_val,
        current_milestone_display=MILESTONE_DISPLAY_NAMES.get(ms_val) if ms_val else None,
        target_go_live=p.target_go_live,
        next_action=p.next_action,
        updated_at=p.updated_at.isoformat() if p.updated_at else None,
        last_activity_at=p.last_activity_at.isoformat() if p.last_activity_at else None,
        operator_id=p.operator_id,
    )


@router.patch("/projects/{project_id}/owner")
def transfer_project_owner(
    project_id: str,
    body: ProjectOwnerUpdate,
    actor: CurrentActor = Depends(get_current_actor),
    conn: sqlite3.Connection = Depends(get_write_conn),
):
    require_permission(actor, P_BUSINESS_WRITE)
    if project_id.startswith("_") or get_project(conn, project_id) is None:
        raise HTTPException(404, f"Project {project_id} not found")
    from src.auth.repository import get_user

    owner = get_user(conn, body.owner_user_id)
    if not owner or owner.get("status") != "active":
        raise HTTPException(422, "Owner must be an active Dashboard user")
    if owner.get("role") not in {ROLE_EDITOR, ROLE_ADMIN}:
        raise HTTPException(422, "Owner must be an active Editor or Admin")
    update_project_fields(conn, project_id, {"operator_id": body.owner_user_id})
    insert_activity(
        conn,
        actor=actor.user_id,
        action_type="owner_transfer",
        project_id=project_id,
        surface="dashboard",
        object_ref=body.owner_user_id,
        summary=f"Owner set to {body.owner_user_id}",
    )
    conn.commit()
    event_bus.publish(Event(
        event_type="project_updated",
        project_id=project_id,
        detail={"fields": ["operator_id"]},
        surface="dashboard",
    ))
    project = get_project(conn, project_id)
    return _to_detail(project)


@router.patch("/projects/{project_id}/notes")
def update_project_notes(
    project_id: str,
    body: ProjectNotesUpdate,
    conn: sqlite3.Connection = Depends(get_write_conn),
):
    """Save operator-written notes for a project."""
    p = get_project(conn, project_id)
    if p is None:
        raise HTTPException(status_code=404, detail=f"Project {project_id} not found")
    update_project_fields(conn, project_id, {"notes": body.notes})
    conn.commit()
    return {"ok": True}


@router.get("/projects/{project_id}/summary", response_model=ProjectSummaryAI)
def get_project_summary(
    project_id: str,
    force: bool = False,
    conn: sqlite3.Connection = Depends(get_conn),
):
    """Return an evidence-aware project briefing."""
    from src.services.project_briefing import get_project_briefing

    briefing = get_project_briefing(conn, project_id, force=force)
    if briefing is None:
        raise HTTPException(status_code=404, detail=f"Project {project_id} not found")
    return ProjectSummaryAI(**briefing)


@router.get("/projects/{project_id}/draft-suggestion", response_model=ProjectDraftSuggestion)
def get_project_draft_suggestion(
    project_id: str,
    conn: sqlite3.Connection = Depends(get_conn),
):
    """Return an AI-suggested email draft for the next client communication."""
    from datetime import datetime, timezone
    from src.email.draft_generator import generate_email_draft
    from src.llm.client import LLMParseFailure

    p = get_project(conn, project_id)
    if p is None:
        raise HTTPException(status_code=404, detail=f"Project {project_id} not found")

    days_stale = 0
    if p.last_activity_at:
        try:
            last_dt = p.last_activity_at
            if last_dt.tzinfo is None:
                last_dt = last_dt.replace(tzinfo=timezone.utc)
            days_stale = max(0, (datetime.now(timezone.utc) - last_dt).days)
        except (ValueError, TypeError):
            pass

    context_hints = {"days_stale": days_stale}

    try:
        subject, body, _ = generate_email_draft(conn, project_id, "stale_external", context_hints)
    except (LLMParseFailure, ValueError, Exception):
        subject = ""
        body = (
            f"Dear {p.client_name or 'Team'},\n\n"
            f"I wanted to follow up regarding {p.next_action or 'our current project status'}. "
            "Could you please provide an update at your earliest convenience?\n\n"
            "Best regards"
        )

    return ProjectDraftSuggestion(draft_body=body, ball_with=p.ball_with.value, subject=subject)


@router.get(
    "/projects/{project_id}/chat",
    response_model=ChatHistoryResponse,
)
def list_project_chat_history(
    project_id: str,
    conn: sqlite3.Connection = Depends(get_conn),
    actor: CurrentActor = Depends(get_current_actor),
):
    """Return the authenticated user's project chat history and capabilities."""
    p = get_project(conn, project_id)
    if p is None:
        raise HTTPException(status_code=404, detail=f"Project {project_id} not found")
    from src.repositories.project_chat_repo import get_project_chat_messages

    read_only = is_read_only_actor(actor)
    messages = get_project_chat_messages(
        conn, project_id, owner_user_id=actor.user_id,
    )
    return ChatHistoryResponse(
        messages=messages,
        capabilities=chat_capabilities(read_only=read_only),
    )


@router.post(
    "/projects/{project_id}/chat",
    response_model=ChatSendResponse,
)
def chat_with_project_agent(
    project_id: str,
    body: ProjectChatRequest,
    conn: sqlite3.Connection = Depends(get_conn),
    actor: CurrentActor = Depends(get_current_actor),
):
    """Run the project agent with backend-enforced actor execution policy."""
    from src.project.web_agent import project_web_agent
    from src.repositories.project_chat_repo import get_project_chat_messages

    if get_project(conn, project_id) is None:
        raise HTTPException(status_code=404, detail=f"Project {project_id} not found")
    read_only = is_read_only_actor(actor)
    if body.session_messages and not read_only:
        raise HTTPException(
            status_code=400,
            detail="session_messages are accepted only for read-only chat sessions",
        )

    active_turn_id = uuid4().hex
    session_messages = (
        normalize_session_messages(body.session_messages) if read_only else []
    )
    try:
        reply = project_web_agent(
            conn,
            project_id,
            body.message,
            use_scoped_writes=True,
            operator_id=actor.user_id,
            dashboard_permissions=actor.permissions,
            read_only_execution=read_only,
            persist_messages=not read_only,
            session_messages=[message.model_dump(exclude_none=True) for message in session_messages],
            turn_id=active_turn_id,
        )
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except Exception:
        logger.exception("project_web_agent failed for %s", project_id)
        raise HTTPException(status_code=500, detail="Agent error: request could not be completed")

    if read_only:
        messages = build_ephemeral_turn(
            session_messages,
            user_message=body.message,
            reply=reply,
            turn_id=active_turn_id,
        )
    else:
        messages = get_project_chat_messages(
            conn, project_id, owner_user_id=actor.user_id,
        )
    return ChatSendResponse(
        reply=reply,
        messages=messages,
        active_turn_id=active_turn_id,
        capabilities=chat_capabilities(read_only=read_only),
    )


# ── Flag ──────────────────────────────────────────────────────────

_VALID_FLAGS = {"urgent", "watch", "clear"}


@router.post("/projects/{project_id}/flag")
def set_project_flag(
    project_id: str,
    body: dict,
    conn: sqlite3.Connection = Depends(get_write_conn),
):
    """Set or clear an operator flag on a project."""
    flag = body.get("flag", "")
    if flag not in _VALID_FLAGS:
        raise HTTPException(status_code=422, detail=f"Invalid flag: {flag}. Must be one of {_VALID_FLAGS}")
    from src.repositories.working_memory_repo import upsert_memory, delete_memory
    if flag == "clear":
        delete_memory(conn, project_id, "operator_flag")
    else:
        upsert_memory(conn, project_id, "operator_flag", flag)
    conn.commit()
    return {"status": "ok", "project_id": project_id, "flag": flag}


def _to_detail(p) -> ProjectDetailResponse:
    ms_val = p.current_milestone.value if p.current_milestone else None
    return ProjectDetailResponse(
        project_id=p.project_id,
        client_name=p.client_name,
        client_email=p.client_email,
        market=p.market,
        project_status=p.project_status.value,
        ball_with=p.ball_with.value,
        current_milestone=ms_val,
        current_milestone_display=MILESTONE_DISPLAY_NAMES.get(ms_val) if ms_val else None,
        target_go_live=p.target_go_live,
        next_action=p.next_action,
        updated_at=p.updated_at.isoformat() if p.updated_at else None,
        last_activity_at=p.last_activity_at.isoformat() if p.last_activity_at else None,
        broker_code=p.broker_code,
        connection_type=p.connection_type,
        notes=p.notes,
        logbook=p.logbook,
        sla_limit=p.sla_limit,
        operator_id=p.operator_id,
        created_at=p.created_at.isoformat() if p.created_at else None,
    )


def _enum_value(value):
    return value.value if hasattr(value, "value") else value


def _to_status_response(status) -> ProjectStatusResponse:
    return ProjectStatusResponse(
        project_id=status.project_id,
        client_name=status.client_name,
        project_status=status.project_status,
        current_milestone=status.current_milestone,
        ball_with=status.ball_with,
        next_action=status.next_action,
        market=status.market,
        context_confidence=status.context_confidence,
        pending_questions=status.pending_questions,
        operator_instruction=status.operator_instruction,
        stale_memory_keys=status.stale_memory_keys,
        skill_observations=status.skill_observations,
        skill_recommendations=status.skill_recommendations,
        recent_evidence_count=status.recent_evidence_count,
        llm_answer=status.llm_answer,
    )


@router.get("/projects/{project_id}/contacts")
def get_project_contacts(
    project_id: str,
    conn: sqlite3.Connection = Depends(get_conn),
):
    """Return all contacts linked to a project via cross_project_history."""
    rows = conn.execute(
        """SELECT contact_id, display_name, primary_email, role, department, created_by
           FROM contacts
           WHERE cross_project_history LIKE ?
           ORDER BY display_name""",
        (f"%{project_id}%",),
    ).fetchall()
    return [
        {
            "contact_id": r["contact_id"],
            "display_name": r["display_name"] or r["primary_email"],
            "primary_email": r["primary_email"],
            "role": r["role"],
            "department": r["department"],
            "confirmed": r["created_by"] == "operator_confirmed",
        }
        for r in rows
    ]
