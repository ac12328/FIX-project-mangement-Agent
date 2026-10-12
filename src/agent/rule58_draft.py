"""
Rule 58 proactive draft — External stall >3 business days.

Creates a review-queue draft (never auto-send) when enabled via config.
Used from post-loop after progress/status conversations.
"""

from __future__ import annotations

import logging
import sqlite3

from src.models.enums import BallWith, ProjectStatus
from src.repositories.project_repo import get_project
from src.utils.calendar import business_days_between
from datetime import datetime, timezone

logger = logging.getLogger(__name__)

_RULE58_STALL_BUSINESS_DAYS = 3


def _business_days_inactive(project) -> int | None:
    if not project.updated_at:
        return None
    updated = project.updated_at
    if isinstance(updated, str):
        updated = datetime.fromisoformat(updated.replace("Z", "+00:00"))
    if updated.tzinfo is None:
        updated = updated.replace(tzinfo=timezone.utc)
    return business_days_between(updated, datetime.now(timezone.utc))


def _has_pending_draft(conn: sqlite3.Connection, project_id: str) -> bool:
    row = conn.execute(
        """
        SELECT 1 FROM drafts
        WHERE project_id = ?
          AND status IN ('created', 'in_review', 'approved')
        LIMIT 1
        """,
        (project_id,),
    ).fetchone()
    return row is not None


def should_offer_rule58_draft(conn: sqlite3.Connection, project_id: str) -> bool:
    """True when wiki rule 58 applies and no pending follow-up draft exists."""
    project = get_project(conn, project_id)
    if not project or project.project_status in ProjectStatus.terminal_statuses():
        return False
    if project.ball_with != BallWith.EXTERNAL:
        return False
    days = _business_days_inactive(project)
    if days is None or days <= _RULE58_STALL_BUSINESS_DAYS:
        return False
    return not _has_pending_draft(conn, project_id)


def try_create_rule58_draft(
    conn: sqlite3.Connection | None,
    project_id: str,
    *,
    chat_id: int,
    wiki_milestone_hint: str | None = None,
) -> str | None:
    """
    Generate stale_external draft if config allows. Returns draft_id or None.
    """
    from src.agent.harness_settings import auto_draft_on_rule58

    if conn is None:
        from src.db.connection import scoped_read

        # Read the persisted operator override as well as the eligibility state.
        # The model and draft engine run only after releasing this read scope;
        # the engine rechecks pending drafts under its own short write scope.
        with scoped_read() as rconn:
            if not auto_draft_on_rule58(rconn) or not should_offer_rule58_draft(rconn, project_id):
                return None
            project = get_project(rconn, project_id)
        return _create(
            None, project_id, chat_id=chat_id,
            wiki_milestone_hint=wiki_milestone_hint, project=project,
        )

    if not auto_draft_on_rule58(conn) or not should_offer_rule58_draft(conn, project_id):
        return None
    return _create(conn, project_id, chat_id=chat_id, wiki_milestone_hint=wiki_milestone_hint)


def _create(
    conn: sqlite3.Connection | None,
    project_id: str,
    *,
    chat_id: int,
    wiki_milestone_hint: str | None,
    project=None,
) -> str | None:
    from src.portfolio.draft_engine import generate_draft_for_signal

    if project is None and conn is not None:
        project = get_project(conn, project_id)
    if not project:
        return None
    days = _business_days_inactive(project) or 0
    milestone = project.current_milestone.value if project.current_milestone else ""
    hints: dict = {
        "days_stale": days,
        "wiki_query": wiki_milestone_hint or f"{milestone} follow-up conformance stall pm-problem-index",
    }
    try:
        draft_id = generate_draft_for_signal(
            conn,
            signal_type="stale_external",
            project_id=project_id,
            context_hints=hints,
            chat_id=chat_id,
        )
        if draft_id:
            logger.info("rule58_draft: created %s for %s", draft_id, project_id)
        return draft_id
    except Exception:
        logger.exception("rule58_draft failed for %s", project_id)
        return None