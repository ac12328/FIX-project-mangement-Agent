"""Dashboard / Morning Brief endpoint."""

import logging
import sqlite3

from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse

from config.defaults import ACTIVE_PROJECT_STATUSES
from src.api.deps import get_conn
from src.api.schemas.dashboard import (
    BriefItemResponse,
    ComingUpItem,
    ComingUpResponse,
    DashboardResponse,
    OverdueFollowupResponse,
)
from src.portfolio.morning_brief import generate_morning_brief

_ACTIVE_PLACEHOLDERS = ",".join("?" for _ in ACTIVE_PROJECT_STATUSES)

router = APIRouter(tags=["dashboard"])
logger = logging.getLogger(__name__)


@router.get("/dashboard", response_model=DashboardResponse)
def get_dashboard(conn: sqlite3.Connection = Depends(get_conn)):
    try:
        brief = generate_morning_brief(conn)

        pending_reviews = conn.execute(
            f"""SELECT COUNT(*) FROM proposed_updates pu
                JOIN projects p ON pu.project_id = p.project_id
                WHERE pu.decision_outcome = 'needs_review'
                  AND p.project_status IN ({_ACTIVE_PLACEHOLDERS})""",
            ACTIVE_PROJECT_STATUSES,
        ).fetchone()[0]
        pending_drafts = conn.execute(
            f"""SELECT COUNT(*) FROM drafts d
                JOIN projects p ON d.project_id = p.project_id
                WHERE d.status IN ('created', 'in_review')
                  AND p.project_status IN ({_ACTIVE_PLACEHOLDERS})""",
            ACTIVE_PROJECT_STATUSES,
        ).fetchone()[0]
        open_followups = conn.execute(
            f"""SELECT COUNT(*) FROM internal_followups f
                JOIN projects p ON f.project_id = p.project_id
                WHERE f.status = 'open'
                  AND p.project_status IN ({_ACTIVE_PLACEHOLDERS})""",
            ACTIVE_PROJECT_STATUSES,
        ).fetchone()[0]

        overdue_followups = conn.execute(
            f"""SELECT COUNT(*) FROM internal_followups f
                JOIN projects p ON f.project_id = p.project_id
                WHERE f.status IN ('open', 'overdue') AND f.due_date < date('now')
                  AND p.project_status IN ({_ACTIVE_PLACEHOLDERS})""",
            ACTIVE_PROJECT_STATUSES,
        ).fetchone()[0]

        due_in_3_days = conn.execute(
            f"""SELECT COUNT(*) FROM (
                 SELECT project_id FROM projects
                 WHERE project_status IN ({_ACTIVE_PLACEHOLDERS})
                   AND target_go_live IS NOT NULL
                   AND target_go_live BETWEEN date('now') AND date('now', '+3 days')
                 UNION ALL
                 SELECT f.project_id FROM internal_followups f
                 JOIN projects p ON f.project_id = p.project_id
                 WHERE f.status = 'open'
                   AND f.due_date BETWEEN date('now') AND date('now', '+3 days')
                   AND p.project_status IN ({_ACTIVE_PLACEHOLDERS})
               )""",
            ACTIVE_PROJECT_STATUSES + ACTIVE_PROJECT_STATUSES,
        ).fetchone()[0]

        def to_response(item):
            return BriefItemResponse(
                project_id=item.project_id,
                client_name=item.client_name,
                current_milestone=item.current_milestone,
                ball_with=item.ball_with,
                next_action=item.next_action,
                urgency=item.urgency,
                urgency_reasons=item.urgency_reasons,
                days_inactive=item.days_inactive,
                pending_drafts=item.pending_drafts,
                open_followups=item.open_followups,
            )

        return DashboardResponse(
            generated_at=brief.generated_at,
            total_active=brief.total_active,
            red_items=[to_response(i) for i in brief.red_items],
            yellow_items=[to_response(i) for i in brief.yellow_items],
            green_items=[to_response(i) for i in brief.green_items],
            pending_reviews_count=pending_reviews,
            pending_drafts_count=pending_drafts,
            open_followups_count=open_followups,
            overdue_followups_count=overdue_followups,
            due_in_3_days_count=due_in_3_days,
        )
    except Exception:
        logger.exception("Failed to build dashboard response")
        return JSONResponse(
            status_code=500,
            content={"error": True, "message": "Could not load dashboard data."},
        )


@router.get("/dashboard/overdue-followups", response_model=list[OverdueFollowupResponse])
def get_overdue_followups(conn: sqlite3.Connection = Depends(get_conn)):
    try:
        rows = conn.execute(
            f"""SELECT f.followup_id, f.project_id, f.description, f.due_date,
                      CAST(julianday('now') - julianday(f.due_date) AS INTEGER) AS days_overdue,
                      p.client_name
               FROM internal_followups f
               LEFT JOIN projects p ON f.project_id = p.project_id
               WHERE f.status IN ('open', 'overdue')
                 AND f.due_date < date('now')
                 AND p.project_status IN ({_ACTIVE_PLACEHOLDERS})
               ORDER BY f.due_date ASC""",
            ACTIVE_PROJECT_STATUSES,
        ).fetchall()
        return [
            OverdueFollowupResponse(
                followup_id=r["followup_id"],
                project_id=r["project_id"],
                client_name=r["client_name"],
                description=r["description"],
                due_date=r["due_date"],
                days_overdue=max(1, r["days_overdue"] or 1),
            )
            for r in rows
        ]
    except Exception:
        logger.exception("Failed to load overdue follow-ups")
        return JSONResponse(
            status_code=500,
            content={"error": True, "message": "Could not load overdue follow-ups."},
        )


@router.get("/dashboard/coming-up", response_model=ComingUpResponse)
def get_coming_up(
    days: int = 3,
    conn: sqlite3.Connection = Depends(get_conn),
):
    try:
        days = max(1, min(days, 14))  # clamp 1-14

        go_live_rows = conn.execute(
            """SELECT p.project_id, p.client_name, p.target_go_live AS due_date,
                      CAST(julianday(p.target_go_live) - julianday(date('now')) AS INTEGER) AS days_until
               FROM projects p
               WHERE p.project_status IN (:s0, :s1, :s2)
                 AND p.target_go_live IS NOT NULL
                 AND p.target_go_live BETWEEN date('now') AND date('now', :offset)
               ORDER BY p.target_go_live ASC""",
            {"s0": ACTIVE_PROJECT_STATUSES[0], "s1": ACTIVE_PROJECT_STATUSES[1],
             "s2": ACTIVE_PROJECT_STATUSES[2], "offset": f"+{days} days"},
        ).fetchall()

        followup_rows = conn.execute(
            """SELECT f.project_id, p.client_name, f.due_date,
                      CAST(julianday(f.due_date) - julianday(date('now')) AS INTEGER) AS days_until
               FROM internal_followups f
               LEFT JOIN projects p ON f.project_id = p.project_id
               WHERE f.status = 'open'
                 AND f.due_date BETWEEN date('now') AND date('now', :offset)
                 AND p.project_status IN (:s0, :s1, :s2)
               ORDER BY f.due_date ASC""",
            {"s0": ACTIVE_PROJECT_STATUSES[0], "s1": ACTIVE_PROJECT_STATUSES[1],
             "s2": ACTIVE_PROJECT_STATUSES[2], "offset": f"+{days} days"},
        ).fetchall()

        def bucket(rows, label_fn):
            today, tomorrow, day2 = [], [], []
            for r in rows:
                d = max(0, r["days_until"] or 0)
                item = ComingUpItem(
                    project_id=r["project_id"],
                    client_name=r["client_name"],
                    label=label_fn(r),
                    due_date=r["due_date"],
                    days_until=d,
                )
                if d == 0:
                    today.append(item)
                elif d == 1:
                    tomorrow.append(item)
                else:
                    day2.append(item)
            return today, tomorrow, day2

        gl_today, gl_tom, gl_d2 = bucket(go_live_rows, lambda _: "Go-live target")
        fu_today, fu_tom, fu_d2 = bucket(followup_rows, lambda _: "Follow-up due")

        return ComingUpResponse(
            today=gl_today + fu_today,
            tomorrow=gl_tom + fu_tom,
            day2=gl_d2 + fu_d2,
        )
    except Exception:
        logger.exception("Failed to load coming-up data")
        return JSONResponse(
            status_code=500,
            content={"error": True, "message": "Could not load coming-up data."},
        )
