"""Operational overview and unified work queue queries for UI API v2."""

from __future__ import annotations

import sqlite3
from datetime import date

from config.defaults import ACTIVE_PROJECT_STATUSES
from src.api.auth import CurrentActor
from src.api.schemas.ui_v2 import (
    ActionItemReadModel,
    RiskItemReadModel,
    RiskSummaryReadModel,
    UpcomingItemReadModel,
)
from src.api.ui_v2_common import _age_days, can_execute
from src.portfolio.morning_brief import generate_morning_brief

_PRIORITY_ORDER = {"critical": 0, "high": 1, "medium": 2, "low": 3}
_HEALTH_ORDER = {"red": 0, "yellow": 1, "green": 2}

def build_risk_summary(conn: sqlite3.Connection) -> RiskSummaryReadModel:
    brief = generate_morning_brief(conn)
    items: list[RiskItemReadModel] = []
    counts = {"red": 0, "yellow": 0, "green": 0}
    for health, projects in (
        ("red", brief.red_items),
        ("yellow", brief.yellow_items),
        ("green", brief.green_items),
    ):
        counts[health] = len(projects)
        for project in projects:
            reasons = [str(reason) for reason in project.urgency_reasons if reason]
            items.append(
                RiskItemReadModel(
                    project_id=project.project_id,
                    project_label=project.client_name or project.project_id,
                    health=health,
                    reason="; ".join(reasons) or "No current risk signal",
                    next_action=project.next_action,
                    milestone=project.current_milestone,
                    days_inactive=project.days_inactive,
                )
            )
    items.sort(
        key=lambda item: (
            _HEALTH_ORDER[item.health],
            -(item.days_inactive or 0),
            item.project_label.lower(),
        )
    )
    return RiskSummaryReadModel(**counts, items=items)


def build_upcoming(conn: sqlite3.Connection, *, limit: int = 12) -> list[UpcomingItemReadModel]:
    active = ",".join("?" for _ in ACTIVE_PROJECT_STATUSES)
    rows = conn.execute(
        f"""
        SELECT 'go_live' AS kind, p.project_id AS id, p.project_id,
               COALESCE(p.client_name, p.project_id) AS project_label,
               'Go-live target' AS title, p.target_go_live AS due_at
        FROM projects p
        WHERE p.project_status IN ({active})
          AND p.project_id NOT LIKE char(92) || '_%' ESCAPE char(92)
          AND p.target_go_live IS NOT NULL
          AND date(p.target_go_live) BETWEEN date('now') AND date('now', '+30 days')
        UNION ALL
        SELECT 'followup' AS kind, f.followup_id AS id, f.project_id,
               COALESCE(p.client_name, f.project_id) AS project_label,
               f.description AS title, f.due_date AS due_at
        FROM internal_followups f
        JOIN projects p ON p.project_id = f.project_id
        WHERE f.status IN ('open', 'overdue')
          AND p.project_status IN ({active})
          AND f.due_date IS NOT NULL
          AND date(f.due_date) BETWEEN date('now') AND date('now', '+30 days')
        ORDER BY due_at ASC
        LIMIT ?
        """,
        [*ACTIVE_PROJECT_STATUSES, *ACTIVE_PROJECT_STATUSES, limit],
    ).fetchall()
    today = date.today()
    result: list[UpcomingItemReadModel] = []
    for row in rows:
        due_at = str(row["due_at"])
        try:
            days_until = (date.fromisoformat(due_at[:10]) - today).days
        except ValueError:
            continue
        kind = str(row["kind"])
        project_id = str(row["project_id"])
        result.append(
            UpcomingItemReadModel(
                id=str(row["id"]),
                kind=kind,
                title=str(row["title"]),
                project_id=project_id,
                project_label=str(row["project_label"]),
                due_at=due_at,
                days_until=days_until,
                href=f"/projects/{project_id}" if kind == "go_live" else "/",
            )
        )
    return result


def build_action_items(conn: sqlite3.Connection, actor: CurrentActor) -> list[ActionItemReadModel]:
    if not can_execute(actor):
        return []

    active = ",".join("?" for _ in ACTIVE_PROJECT_STATUSES)
    items: list[ActionItemReadModel] = []

    from src.project_reconciliation.repository import proposed_changes_are_notes_only

    review_rows = conn.execute(
        f"""SELECT pu.update_id, pu.project_id, pu.task_type, pu.risk_level,
                    pu.created_at, p.client_name, pu.proposed_changes,
                    po.trigger_kind, po.trusted_surface
             FROM proposed_updates pu
             JOIN projects p ON p.project_id = pu.project_id
             LEFT JOIN pending_operations po ON po.operation_id = pu.operation_id
             WHERE pu.decision_outcome = 'needs_review'
               AND p.project_status IN ({active})""",
        ACTIVE_PROJECT_STATUSES,
    ).fetchall()
    for row in review_rows:
        task_type_raw = str(row["task_type"] or "")
        if task_type_raw == "email_reconciliation":
            if proposed_changes_are_notes_only(row["proposed_changes"]):
                continue
            if (
                str(row["trigger_kind"] or "") == "scheduler"
                or str(row["trusted_surface"] or "") == "scheduler"
            ):
                continue
        risk = str(row["risk_level"] or "medium").lower()
        priority = risk if risk in _PRIORITY_ORDER else "medium"
        project_id = str(row["project_id"])
        task_type = (task_type_raw or "project update").replace("_", " ")
        items.append(
            ActionItemReadModel(
                id=str(row["update_id"]),
                kind="review",
                priority=priority,
                status="needs_review",
                title=f"Review {task_type}",
                reason="A proposed project change needs an operator decision.",
                project_id=project_id,
                project_label=row["client_name"] or project_id,
                created_at=row["created_at"],
                age_days=_age_days(row["created_at"]),
                action_href="/reviews",
                action_label="Review",
                can_execute=True,
            )
        )

    draft_rows = conn.execute(
        f"""SELECT d.draft_id, d.project_id, d.subject, d.status, d.delivery_status,
                    d.created_at, d.scheduled_send_at, p.client_name
             FROM drafts d
             LEFT JOIN projects p ON p.project_id = d.project_id
             WHERE d.status IN ('created', 'in_review')
               AND (p.project_status IN ({active}) OR d.project_id IS NULL)""",
        ACTIVE_PROJECT_STATUSES,
    ).fetchall()
    for row in draft_rows:
        delivery_status = str(row["delivery_status"] or "editing")
        priority = "high" if delivery_status == "needs_attention" else "medium"
        project_id = str(row["project_id"]) if row["project_id"] else None
        items.append(
            ActionItemReadModel(
                id=str(row["draft_id"]),
                kind="draft",
                priority=priority,
                status=str(row["status"]),
                title=str(row["subject"] or "Untitled draft"),
                reason="A draft is waiting for review or delivery.",
                project_id=project_id,
                project_label=(row["client_name"] or project_id) if project_id else "Standalone",
                created_at=row["created_at"],
                due_at=row["scheduled_send_at"],
                age_days=_age_days(row["created_at"]),
                action_href=f"/drafts/{row['draft_id']}",
                action_label="Open draft",
                can_execute=True,
            )
        )

    approval_rows = conn.execute(
        """SELECT action_id, action_type, risk_class, target_type, target_id,
                  status, created_at, expires_at
             FROM action_intents
             WHERE operator_id = ? AND status = 'pending'""",
        (actor.operator_id,),
    ).fetchall()
    for row in approval_rows:
        risk = str(row["risk_class"] or "medium").lower()
        priority = risk if risk in _PRIORITY_ORDER else "medium"
        action_type_raw = str(row["action_type"])
        target_id = str(row["target_id"])
        action_type = action_type_raw.replace("_", " ")
        project_completion = action_type_raw == "project_completion" or str(row["target_type"]) == "project"
        items.append(
            ActionItemReadModel(
                id=str(row["action_id"]),
                kind="approval",
                priority=priority,
                status=str(row["status"]),
                title=f"Approve {action_type}",
                reason=f"{row['target_type']} {target_id} requires approval before execution.",
                created_at=row["created_at"],
                due_at=row["expires_at"],
                age_days=_age_days(row["created_at"]),
                action_href=f"/projects/{target_id}" if project_completion else "/approvals",
                action_label="Review approval",
                can_execute=True,
            )
        )

    followup_rows = conn.execute(
        f"""SELECT f.followup_id, f.project_id, f.description, f.status,
                    f.due_date, f.created_at, p.client_name,
                    CAST(julianday(date('now')) - julianday(date(f.due_date)) AS INTEGER) AS overdue_days
             FROM internal_followups f
             JOIN projects p ON p.project_id = f.project_id
             WHERE f.status IN ('open', 'overdue')
               AND p.project_status IN ({active})""",
        ACTIVE_PROJECT_STATUSES,
    ).fetchall()
    for row in followup_rows:
        overdue_days = int(row["overdue_days"] or 0)
        priority = "high" if overdue_days > 0 else "medium" if overdue_days >= -3 else "low"
        project_id = str(row["project_id"])
        items.append(
            ActionItemReadModel(
                id=str(row["followup_id"]),
                kind="followup",
                priority=priority,
                status=str(row["status"]),
                title=str(row["description"]),
                reason=(
                    f"Follow-up is {overdue_days} day{'s' if overdue_days != 1 else ''} overdue."
                    if overdue_days > 0
                    else "A project follow-up is due soon."
                ),
                project_id=project_id,
                project_label=row["client_name"] or project_id,
                created_at=row["created_at"],
                due_at=row["due_date"],
                age_days=_age_days(row["created_at"]),
                action_href=f"/projects/{project_id}",
                action_label="Open project",
                can_execute=True,
            )
        )

    items.sort(
        key=lambda item: (
            _PRIORITY_ORDER[item.priority],
            item.due_at or "9999-12-31",
            -(item.age_days or 0),
            item.title.lower(),
        )
    )
    return items


def filter_action_items(
    items: list[ActionItemReadModel],
    *,
    kind: str | None,
    priority: str | None,
    status: str | None,
    sort: str,
) -> list[ActionItemReadModel]:
    filtered = [
        item
        for item in items
        if (not kind or item.kind == kind)
        and (not priority or item.priority == priority)
        and (not status or item.status == status)
    ]
    if sort == "oldest":
        filtered.sort(key=lambda item: (-(item.age_days or 0), item.title.lower()))
    elif sort == "due":
        filtered.sort(key=lambda item: (item.due_at or "9999-12-31", _PRIORITY_ORDER[item.priority]))
    else:
        filtered.sort(key=lambda item: (_PRIORITY_ORDER[item.priority], item.due_at or "9999-12-31"))
    return filtered
