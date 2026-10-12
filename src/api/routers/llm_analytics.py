"""LLM analytics endpoints — summary, per-task breakdown, daily trends."""

import sqlite3

from fastapi import APIRouter, Depends, Query

from src.api.deps import get_conn
from src.api.schemas.llm_analytics import (
    DailyTrend,
    LLMSummary,
    TaskBreakdown,
    TaskBreakdownResponse,
    TrendsResponse,
)

router = APIRouter(prefix="/llm-analytics", tags=["llm-analytics"])


@router.get("/summary", response_model=LLMSummary)
def get_summary(
    days: int = Query(7, ge=1, le=90),
    conn: sqlite3.Connection = Depends(get_conn),
) -> LLMSummary:
    """Aggregate LLM usage stats for the last N days."""
    row = conn.execute(
        """
        SELECT
            COUNT(*)                                   AS total_calls,
            COALESCE(SUM(tokens_in + tokens_out), 0)   AS total_tokens,
            COALESCE(AVG(latency_ms), 0.0)             AS avg_latency_ms,
            COALESCE(SUM(
                CASE WHEN error_class IS NOT NULL AND error_class != 'none'
                     THEN 1 ELSE 0 END
            ), 0)                                      AS error_count
        FROM llm_traces
        WHERE created_at >= datetime('now', ?)
        """,
        (f"-{days} days",),
    ).fetchone()

    total_calls = row[0]
    error_count = row[3]
    error_rate = (error_count / total_calls * 100.0) if total_calls > 0 else 0.0

    return LLMSummary(
        total_calls=total_calls,
        total_tokens=row[1],
        avg_latency_ms=round(row[2], 2),
        error_count=error_count,
        error_rate_pct=round(error_rate, 2),
        period_days=days,
    )


@router.get("/by-task", response_model=TaskBreakdownResponse)
def get_by_task(
    days: int = Query(7, ge=1, le=90),
    conn: sqlite3.Connection = Depends(get_conn),
) -> TaskBreakdownResponse:
    """Per-task breakdown of LLM usage for the last N days."""
    rows = conn.execute(
        """
        SELECT
            task,
            COUNT(*)                                   AS cnt,
            COALESCE(AVG(tokens_in + tokens_out), 0.0) AS avg_tokens,
            COALESCE(AVG(latency_ms), 0.0)             AS avg_latency_ms,
            COALESCE(SUM(
                CASE WHEN error_class IS NOT NULL AND error_class != 'none'
                     THEN 1 ELSE 0 END
            ), 0)                                      AS error_count
        FROM llm_traces
        WHERE created_at >= datetime('now', ?)
        GROUP BY task
        ORDER BY cnt DESC
        """,
        (f"-{days} days",),
    ).fetchall()

    tasks = [
        TaskBreakdown(
            task=r[0],
            count=r[1],
            avg_tokens=round(r[2], 2),
            avg_latency_ms=round(r[3], 2),
            error_count=r[4],
        )
        for r in rows
    ]
    return TaskBreakdownResponse(tasks=tasks)


@router.get("/trends", response_model=TrendsResponse)
def get_trends(
    days: int = Query(7, ge=1, le=90),
    conn: sqlite3.Connection = Depends(get_conn),
) -> TrendsResponse:
    """Daily aggregated LLM usage for the last N days."""
    rows = conn.execute(
        """
        SELECT
            DATE(created_at)                           AS day,
            COUNT(*)                                   AS calls,
            COALESCE(SUM(tokens_in + tokens_out), 0)   AS tokens,
            COALESCE(SUM(
                CASE WHEN error_class IS NOT NULL AND error_class != 'none'
                     THEN 1 ELSE 0 END
            ), 0)                                      AS errors,
            COALESCE(AVG(latency_ms), 0.0)             AS avg_latency_ms
        FROM llm_traces
        WHERE created_at >= datetime('now', ?)
        GROUP BY DATE(created_at)
        ORDER BY day ASC
        """,
        (f"-{days} days",),
    ).fetchall()

    trend_days = [
        DailyTrend(
            date=r[0],
            calls=r[1],
            tokens=r[2],
            errors=r[3],
            avg_latency_ms=round(r[4], 2),
        )
        for r in rows
    ]
    return TrendsResponse(days=trend_days)
