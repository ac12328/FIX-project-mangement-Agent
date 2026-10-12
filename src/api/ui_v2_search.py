"""Permission-aware command palette search for Dashboard UI API v2."""

from __future__ import annotations

import sqlite3

from src.api.auth import CurrentActor
from src.api.schemas.ui_v2 import SearchItemReadModel
from src.auth.rbac import P_BUSINESS_WRITE

_PAGE_SEARCH_ITEMS: tuple[tuple[str, str, str, str | None, tuple[str, ...]], ...] = (
    ("page-home", "Today", "/", "shared.read", ("dashboard", "overview", "attention", "home")),
    ("page-ask", "Ask", "/ask", "assistant.read", ("queries", "assistant", "ai")),
    ("page-mail", "Mail", "/mail", "business.write", ("inbox", "drafts", "compose", "email", "messages")),
    ("page-reviews", "Reviews", "/reviews", "business.write", ("queue", "approve", "reject")),
    ("page-projects", "Projects", "/projects", "shared.read", ("portfolio", "onboarding", "intake")),
    ("page-operations", "Operations", "/operations", "shared.read", ("portfolio", "health", "timeline", "milestone", "go live")),
    ("page-counterparties", "Counterparties", "/counterparties", "shared.read", ("clients", "vendors", "parties", "organisations", "counterparty")),
    ("page-support", "Desk issues", "/support", "business.write", ("tickets", "issues", "support")),
    ("page-analytics", "LLM Analytics", "/llm-analytics", "shared.read", ("cost", "latency")),
    ("page-learning", "Learning", "/learning", "admin.learning_governance", ("memory", "candidates")),
    ("page-admin", "Admin Console", "/admin", "admin.users", ("users", "security")),
)


def search_items(
    conn: sqlite3.Connection,
    actor: CurrentActor,
    *,
    query: str,
    types: set[str] | None,
    limit: int,
) -> list[SearchItemReadModel]:
    normalized = query.strip().lower()
    pattern = f"%{query.strip()}%"
    items: list[SearchItemReadModel] = []

    def wanted(item_type: str) -> bool:
        return not types or item_type in types

    if wanted("page"):
        for item_id, title, href, permission, keywords in _PAGE_SEARCH_ITEMS:
            if permission and permission not in actor.permissions:
                continue
            haystack = " ".join((title, *keywords)).lower()
            if normalized and normalized not in haystack:
                continue
            score = 1.0 if title.lower().startswith(normalized) else 0.72
            items.append(
                SearchItemReadModel(
                    id=item_id,
                    type="page",
                    title=title,
                    subtitle="Page",
                    href=href,
                    keywords=list(keywords),
                    score=score,
                )
            )

    if wanted("project"):
        rows = conn.execute(
            """SELECT project_id, client_name, market FROM projects
               WHERE project_id NOT LIKE char(92) || '_%' ESCAPE char(92)
                 AND (project_id LIKE ? OR client_name LIKE ? OR market LIKE ?)
               LIMIT 30""",
            (pattern, pattern, pattern),
        ).fetchall()
        for row in rows:
            title = str(row["client_name"] or row["project_id"])
            items.append(
                SearchItemReadModel(
                    id=str(row["project_id"]),
                    type="project",
                    title=title,
                    subtitle=f"Project {row['project_id']}",
                    href=f"/projects/{row['project_id']}",
                    keywords=[str(row["market"] or "")],
                    score=0.95 if title.lower().startswith(normalized) else 0.7,
                )
            )

    if wanted("client") or wanted("vendor"):
        rows = conn.execute(
            """SELECT org_id, org_name, org_type FROM organisations
               WHERE (org_name LIKE ? OR domains LIKE ?)
                 AND org_type IN ('client', 'vendor')
               LIMIT 30""",
            (pattern, pattern),
        ).fetchall()
        for row in rows:
            item_type = str(row["org_type"])
            if not wanted(item_type):
                continue
            items.append(
                SearchItemReadModel(
                    id=str(row["org_id"]),
                    type=item_type,
                    title=str(row["org_name"]),
                    subtitle=item_type.title(),
                    href=f"/parties/{row['org_id']}",
                    score=0.9 if str(row["org_name"]).lower().startswith(normalized) else 0.66,
                )
            )

    if wanted("ticket") and P_BUSINESS_WRITE in actor.permissions:
        rows = conn.execute(
            """SELECT ticket_id, title, severity, status FROM support_tickets
               WHERE ticket_id LIKE ? OR title LIKE ? OR description LIKE ?
               LIMIT 30""",
            (pattern, pattern, pattern),
        ).fetchall()
        for row in rows:
            items.append(
                SearchItemReadModel(
                    id=str(row["ticket_id"]),
                    type="ticket",
                    title=str(row["title"]),
                    subtitle=f"{row['severity']} · {row['status']}",
                    href=f"/support/{row['ticket_id']}",
                    score=0.88 if str(row["title"]).lower().startswith(normalized) else 0.64,
                )
            )

    items.sort(key=lambda item: (-item.score, item.title.lower()))
    return items[:limit]
