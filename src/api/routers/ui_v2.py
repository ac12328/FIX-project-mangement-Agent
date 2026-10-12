"""Additive, role-aware read-model endpoints for the redesigned Dashboard UI."""

from __future__ import annotations

import logging
import sqlite3
from collections import Counter
from typing import Literal, Sequence, TypeVar

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import JSONResponse

from src.api.auth import CurrentActor
from src.api.deps import get_current_actor, get_read_conn
from src.api.schemas.ui_v2 import (
    ActionQueueCounts,
    ActivityResponse,
    DraftsResponse,
    FacetReadModel,
    FacetValue,
    MetricReadModel,
    PartiesResponse,
    ProblemResponse,
    CompletedProjectsResponse,
    ProjectsResponse,
    ReviewsResponse,
    RiskSummaryReadModel,
    SectionStatus,
    SupportResponse,
    TimelineResponse,
    UiOverviewResponse,
    UiSearchResponse,
    WorkItemsResponse,
)
from src.api.ui_v2_common import can_execute, utc_now
from src.api.ui_v2_completed import list_completed_projects
from src.api.ui_v2_lists import (
    list_activity,
    list_drafts,
    list_parties,
    list_projects,
    list_reviews,
    list_support,
    list_timeline,
)
from src.api.ui_v2_overview import (
    build_action_items,
    build_risk_summary,
    build_upcoming,
    filter_action_items,
)
from src.api.ui_v2_search import search_items

router = APIRouter(prefix="/ui/v2", tags=["ui-v2"])
logger = logging.getLogger(__name__)
T = TypeVar("T")

_PROBLEM_RESPONSES = {
    500: {"model": ProblemResponse, "description": "Read model generation failed"},
}


def _paginate(items: Sequence[T], page: int, page_size: int) -> tuple[list[T], int]:
    total = len(items)
    start = (page - 1) * page_size
    return list(items[start : start + page_size]), total


def _problem(request: Request, exc: Exception) -> JSONResponse:
    logger.exception("UI v2 read model failed: %s", request.url.path)
    payload = ProblemResponse(
        type="urn:agentfixbpro:problem:read-model",
        title="Could not load workspace data",
        status=500,
        detail="The requested read model could not be generated.",
        instance=request.url.path,
    )
    return JSONResponse(status_code=500, content=payload.model_dump())


def _section_status(section: str, error: Exception | None = None) -> SectionStatus:
    return SectionStatus(
        section=section,
        state="error" if error else "ok",
        message="This panel could not be refreshed." if error else None,
        generated_at=utc_now(),
    )


def _action_facets(items) -> list[FacetReadModel]:
    result: list[FacetReadModel] = []
    for facet_id, label, values in (
        ("kind", "Kind", (item.kind for item in items)),
        ("priority", "Priority", (item.priority for item in items)),
        ("status", "Status", (item.status for item in items)),
    ):
        counts = Counter(values)
        result.append(
            FacetReadModel(
                id=facet_id,
                label=label,
                values=[
                    FacetValue(value=value, count=count)
                    for value, count in sorted(counts.items(), key=lambda item: (-item[1], item[0]))
                ],
            )
        )
    return result


@router.get("/overview", response_model=UiOverviewResponse, responses=_PROBLEM_RESPONSES)
def get_overview(
    actor: CurrentActor = Depends(get_current_actor),
    conn: sqlite3.Connection = Depends(get_read_conn),
) -> UiOverviewResponse:
    generated_at = utc_now()
    statuses: list[SectionStatus] = []

    try:
        risk = build_risk_summary(conn)
        statuses.append(_section_status("risk_summary"))
    except Exception as exc:
        logger.exception("UI v2 risk summary failed")
        risk = RiskSummaryReadModel()
        statuses.append(_section_status("risk_summary", exc))

    queue_counts = None
    try:
        queue = build_action_items(conn, actor)
        queue_counts = ActionQueueCounts(**Counter(item.kind for item in queue))
        statuses.append(_section_status("action_queue"))
    except Exception as exc:
        logger.exception("UI v2 action queue failed")
        queue = []
        statuses.append(_section_status("action_queue", exc))

    try:
        upcoming = build_upcoming(conn)
        statuses.append(_section_status("upcoming"))
    except Exception as exc:
        logger.exception("UI v2 upcoming failed")
        upcoming = []
        statuses.append(_section_status("upcoming", exc))

    total_health = risk.red + risk.yellow + risk.green
    health_pct = round((risk.green / total_health) * 100) if total_health else 0
    metrics = [
        MetricReadModel(
            id="needs_attention",
            label="Needs attention",
            value=risk.red,
            detail="Projects with an active risk signal",
            tone="danger" if risk.red else "neutral",
            href="/projects?health=red",
        ),
        MetricReadModel(
            id="my_work",
            label="My work",
            value=len(queue),
            detail="Reviews, drafts, approvals, and follow-ups",
            tone="warning" if queue else "neutral",
            href="/?queue=open#action-queue",
        ),
        MetricReadModel(
            id="portfolio_health",
            label="Portfolio health",
            value=f"{health_pct}%",
            detail=f"{risk.green} of {total_health} active projects on track",
            tone="success" if total_health and health_pct >= 70 else "warning" if total_health else "neutral",
            href="/operations?tab=health",
        ),
    ]
    return UiOverviewResponse(
        generated_at=generated_at,
        role=actor.role,
        can_execute=can_execute(actor),
        metrics=metrics,
        action_queue=queue[:12],
        action_queue_total=len(queue),
        action_queue_counts=queue_counts,
        risk_summary=risk,
        upcoming=upcoming,
        section_status=statuses,
    )


@router.get("/search", response_model=UiSearchResponse, responses=_PROBLEM_RESPONSES)
def get_search(
    request: Request,
    q: str = Query(..., min_length=1, max_length=120),
    types: str | None = Query(None),
    limit: int = Query(12, ge=1, le=50),
    actor: CurrentActor = Depends(get_current_actor),
    conn: sqlite3.Connection = Depends(get_read_conn),
):
    try:
        selected_types = {value.strip() for value in types.split(",") if value.strip()} if types else None
        return UiSearchResponse(
            items=search_items(conn, actor, query=q, types=selected_types, limit=limit),
            query=q,
            generated_at=utc_now(),
        )
    except Exception as exc:
        return _problem(request, exc)


@router.get("/work-items", response_model=WorkItemsResponse, responses=_PROBLEM_RESPONSES)
def get_work_items(
    request: Request,
    kind: Literal["review", "draft", "approval", "followup"] | None = None,
    priority: Literal["critical", "high", "medium", "low"] | None = None,
    status: str | None = None,
    sort: Literal["priority", "due", "oldest"] = "priority",
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    actor: CurrentActor = Depends(get_current_actor),
    conn: sqlite3.Connection = Depends(get_read_conn),
):
    try:
        all_items = build_action_items(conn, actor)
        filtered = filter_action_items(
            all_items,
            kind=kind,
            priority=priority,
            status=status,
            sort=sort,
        )
        page_items, total = _paginate(filtered, page, page_size)
        return WorkItemsResponse(
            items=page_items,
            facets=_action_facets(all_items),
            page=page,
            page_size=page_size,
            total=total,
            generated_at=utc_now(),
        )
    except Exception as exc:
        return _problem(request, exc)


@router.get("/projects", response_model=ProjectsResponse, responses=_PROBLEM_RESPONSES)
def get_projects(
    request: Request,
    q: str | None = None,
    status: str | None = None,
    ball_with: str | None = None,
    milestone: str | None = None,
    market: str | None = None,
    health: Literal["red", "yellow", "green"] | None = None,
    active_only: bool = True,
    mine: bool = False,
    owner: str | None = None,
    sort: Literal["risk", "name", "updated", "go_live"] = "risk",
    page: int = Query(1, ge=1),
    page_size: int = Query(25, ge=1, le=100),
    actor: CurrentActor = Depends(get_current_actor),
    conn: sqlite3.Connection = Depends(get_read_conn),
):
    try:
        items, facets = list_projects(
            conn,
            q=q,
            status=status,
            ball_with=ball_with,
            milestone=milestone,
            market=market,
            health=health,
            active_only=active_only,
            sort=sort,
            owner=actor.user_id if mine else owner,
        )
        page_items, total = _paginate(items, page, page_size)
        return ProjectsResponse(
            items=page_items,
            facets=facets,
            page=page,
            page_size=page_size,
            total=total,
            generated_at=utc_now(),
        )
    except Exception as exc:
        return _problem(request, exc)


@router.get("/completed-projects", response_model=CompletedProjectsResponse, responses=_PROBLEM_RESPONSES)
def get_completed_projects(
    request: Request,
    days: int = Query(30, description="Look back 14 or 30 days from now"),
    q: str | None = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(25, ge=1, le=100),
    conn: sqlite3.Connection = Depends(get_read_conn),
):
    if days not in (14, 30):
        raise HTTPException(status_code=422, detail="days must be 14 or 30")
    try:
        items, facets = list_completed_projects(conn, days=days, q=q)
        page_items, total = _paginate(items, page, page_size)
        return CompletedProjectsResponse(
            items=page_items,
            facets=facets,
            page=page,
            page_size=page_size,
            total=total,
            generated_at=utc_now(),
            days=days,
        )
    except Exception as exc:
        return _problem(request, exc)


@router.get("/parties", response_model=PartiesResponse, responses=_PROBLEM_RESPONSES)
def get_parties(
    request: Request,
    org_type: Literal["client", "vendor", "regulator", "internal", "unknown"] | None = None,
    q: str | None = None,
    sort: Literal["name", "projects", "recent"] = "name",
    page: int = Query(1, ge=1),
    page_size: int = Query(25, ge=1, le=100),
    conn: sqlite3.Connection = Depends(get_read_conn),
):
    try:
        items, facets = list_parties(conn, org_type=org_type, q=q, sort=sort)
        page_items, total = _paginate(items, page, page_size)
        return PartiesResponse(
            items=page_items,
            facets=facets,
            page=page,
            page_size=page_size,
            total=total,
            generated_at=utc_now(),
        )
    except Exception as exc:
        return _problem(request, exc)


@router.get("/support", response_model=SupportResponse, responses=_PROBLEM_RESPONSES)
def get_support(
    request: Request,
    q: str | None = None,
    status: str | None = None,
    severity: str | None = None,
    queue: str | None = None,
    case_type: str | None = None,
    unidentified_only: bool = False,
    related_project_id: str | None = None,
    sort: Literal["priority", "updated", "title"] = "priority",
    page: int = Query(1, ge=1),
    page_size: int = Query(25, ge=1, le=100),
    actor: CurrentActor = Depends(get_current_actor),
    conn: sqlite3.Connection = Depends(get_read_conn),
):
    try:
        items, facets, queue_counts = list_support(
            conn,
            actor,
            q=q,
            status=status,
            severity=severity,
            sort=sort,
            queue=queue,
            case_type=case_type,
            unidentified_only=unidentified_only,
            related_project_id=related_project_id,
        )
        page_items, total = _paginate(items, page, page_size)
        return SupportResponse(
            items=page_items,
            facets=facets,
            page=page,
            page_size=page_size,
            total=total,
            generated_at=utc_now(),
            queue_counts=queue_counts,
        )
    except Exception as exc:
        return _problem(request, exc)


@router.get("/drafts", response_model=DraftsResponse, responses=_PROBLEM_RESPONSES)
def get_drafts(
    request: Request,
    status: str | None = None,
    q: str | None = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(25, ge=1, le=100),
    actor: CurrentActor = Depends(get_current_actor),
    conn: sqlite3.Connection = Depends(get_read_conn),
):
    try:
        items, facets = list_drafts(conn, actor, status=status, q=q)
        page_items, total = _paginate(items, page, page_size)
        return DraftsResponse(
            items=page_items,
            facets=facets,
            page=page,
            page_size=page_size,
            total=total,
            generated_at=utc_now(),
        )
    except Exception as exc:
        return _problem(request, exc)


@router.get("/reviews", response_model=ReviewsResponse, responses=_PROBLEM_RESPONSES)
def get_reviews(
    request: Request,
    risk: str | None = None,
    q: str | None = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(25, ge=1, le=100),
    actor: CurrentActor = Depends(get_current_actor),
    conn: sqlite3.Connection = Depends(get_read_conn),
):
    try:
        items, facets = list_reviews(conn, actor, risk=risk, q=q)
        page_items, total = _paginate(items, page, page_size)
        return ReviewsResponse(
            items=page_items,
            facets=facets,
            page=page,
            page_size=page_size,
            total=total,
            generated_at=utc_now(),
        )
    except Exception as exc:
        return _problem(request, exc)


@router.get("/timeline", response_model=TimelineResponse, responses=_PROBLEM_RESPONSES)
def get_ui_timeline(
    request: Request,
    q: str | None = None,
    health: Literal["red", "yellow", "green"] | None = None,
    schedule_status: Literal["overdue", "due_soon", "scheduled", "no_date", "invalid"] | None = None,
    sort: Literal["milestone", "go_live", "risk", "name"] = "milestone",
    page: int = Query(1, ge=1),
    page_size: int = Query(25, ge=1, le=100),
    conn: sqlite3.Connection = Depends(get_read_conn),
):
    try:
        items, facets = list_timeline(
            conn,
            q=q,
            health=health,
            schedule_status=schedule_status,
            sort=sort,
        )
        page_items, total = _paginate(items, page, page_size)
        return TimelineResponse(
            items=page_items,
            facets=facets,
            page=page,
            page_size=page_size,
            total=total,
            generated_at=utc_now(),
        )
    except Exception as exc:
        return _problem(request, exc)


@router.get("/activity", response_model=ActivityResponse, responses=_PROBLEM_RESPONSES)
def get_activity(
    request: Request,
    action_type: str | None = None,
    project_id: str | None = None,
    q: str | None = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(50, ge=1, le=100),
    conn: sqlite3.Connection = Depends(get_read_conn),
):
    try:
        items, facets = list_activity(
            conn,
            action_type=action_type,
            project_id=project_id,
            q=q,
        )
        page_items, total = _paginate(items, page, page_size)
        return ActivityResponse(
            items=page_items,
            facets=facets,
            page=page,
            page_size=page_size,
            total=total,
            generated_at=utc_now(),
        )
    except Exception as exc:
        return _problem(request, exc)
