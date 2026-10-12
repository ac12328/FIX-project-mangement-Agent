"""Aggregate query endpoint."""

import csv
import io
import sqlite3

from fastapi import APIRouter, Depends, Query
from fastapi.responses import StreamingResponse

from src.api.deps import get_conn
from src.api.schemas.query import (
    AggregateQueryResponse,
    AggregateQueryRowResponse,
    QueryContextProfileResponse,
    QueryDateRangeResponse,
    QueryScopeResponse,
)
from src.models.enums import MILESTONE_DISPLAY_NAMES
from src.portfolio.aggregate_query import evaluate_aggregate_query

router = APIRouter(tags=["query"])


@router.get("/query")
def run_aggregate_query(
    text: str = Query(..., min_length=1),
    limit: int = Query(20, ge=1, le=100),
    offset: int = Query(0, ge=0),
    format: str = Query("json", pattern="^(json|csv)$"),
    conn: sqlite3.Connection = Depends(get_conn),
):
    evaluation = evaluate_aggregate_query(conn, text, limit=limit + offset)
    result = evaluation.result
    all_rows = [_to_row(row) for row in (result.results if result else [])]
    paginated_rows = all_rows[offset:offset + limit]

    if format == "csv":
        return _rows_to_csv(paginated_rows)

    return AggregateQueryResponse(
        status=evaluation.status,
        clarification_question=evaluation.clarification_question,
        profile=_to_profile(evaluation.profile),
        suggested_filters={key: str(value) for key, value in evaluation.suggested_filters.items()},
        filters_applied={key: value for key, value in (result.filters_applied if result else {}).items()},
        total=result.total if result else 0,
        results=paginated_rows,
    )


def _rows_to_csv(rows: list[AggregateQueryRowResponse]) -> StreamingResponse:
    fields = ["project_id", "client_name", "market", "project_status",
              "ball_with", "current_milestone", "current_milestone_display", "next_action"]
    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=fields, extrasaction="ignore")
    writer.writeheader()
    for row in rows:
        writer.writerow({f: getattr(row, f, "") or "" for f in fields})
    buf.seek(0)
    return StreamingResponse(
        iter([buf.getvalue()]),
        media_type="text/csv",
        headers={"Content-Disposition": "attachment; filename=query_results.csv"},
    )


def _to_profile(profile) -> QueryContextProfileResponse:
    return QueryContextProfileResponse(
        task_type=profile.task_type,
        query_text=profile.query_text,
        generated_at=profile.generated_at,
        scope=QueryScopeResponse(
            project_filter=profile.scope.project_filter,
            date_range=QueryDateRangeResponse(
                from_date=profile.scope.date_range.from_date,
                to_date=profile.scope.date_range.to_date,
            ),
            contact_filter=profile.scope.contact_filter,
        ),
        fields_requested=profile.fields_requested,
        sort_order=profile.sort_order,
        limit=profile.limit,
        confidence=profile.confidence,
        escalate_to_operator=profile.escalate_to_operator,
        escalation_reason=profile.escalation_reason,
    )


def _to_row(row: dict) -> AggregateQueryRowResponse:
    milestone = row.get("current_milestone")
    return AggregateQueryRowResponse(
        project_id=row["project_id"],
        client_name=row.get("client_name"),
        market=row.get("market"),
        project_status=row.get("project_status"),
        ball_with=row.get("ball_with"),
        current_milestone=milestone,
        current_milestone_display=MILESTONE_DISPLAY_NAMES.get(milestone) if milestone else None,
        next_action=row.get("next_action"),
    )



def _to_profile(profile) -> QueryContextProfileResponse:
    return QueryContextProfileResponse(
        task_type=profile.task_type,
        query_text=profile.query_text,
        generated_at=profile.generated_at,
        scope=QueryScopeResponse(
            project_filter=profile.scope.project_filter,
            date_range=QueryDateRangeResponse(
                from_date=profile.scope.date_range.from_date,
                to_date=profile.scope.date_range.to_date,
            ),
            contact_filter=profile.scope.contact_filter,
        ),
        fields_requested=profile.fields_requested,
        sort_order=profile.sort_order,
        limit=profile.limit,
        confidence=profile.confidence,
        escalate_to_operator=profile.escalate_to_operator,
        escalation_reason=profile.escalation_reason,
    )


def _to_row(row: dict) -> AggregateQueryRowResponse:
    milestone = row.get("current_milestone")
    return AggregateQueryRowResponse(
        project_id=row["project_id"],
        client_name=row.get("client_name"),
        market=row.get("market"),
        project_status=row.get("project_status"),
        ball_with=row.get("ball_with"),
        current_milestone=milestone,
        current_milestone_display=MILESTONE_DISPLAY_NAMES.get(milestone) if milestone else None,
        next_action=row.get("next_action"),
    )
