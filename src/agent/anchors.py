"""DB-sourced loop anchors reinjected each agent round."""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass

from src.repositories.project_repo import get_project
from src.repositories.working_memory_repo import get_memory

_PENDING_KEY = "pending_chat_update"


@dataclass(frozen=True)
class LoopAnchors:
    project_id: str | None
    current_milestone: str | None
    ball_with: str | None
    project_status: str | None
    has_pending_staged_update: bool = False


def extract_anchors(
    conn: sqlite3.Connection | None,
    *,
    effective_project_id: str | None,
) -> LoopAnchors:
    if not effective_project_id:
        return LoopAnchors(None, None, None, None, False)
    if conn is None:
        from src.db.connection import scoped_read

        with scoped_read() as rconn:
            return extract_anchors(
                rconn,
                effective_project_id=effective_project_id,
            )

    project = get_project(conn, effective_project_id)
    if project is None:
        return LoopAnchors(effective_project_id, None, None, None, False)

    pending = get_memory(conn, effective_project_id, _PENDING_KEY) is not None

    milestone = project.current_milestone
    ball_with = project.ball_with
    status = project.project_status

    return LoopAnchors(
        project_id=effective_project_id,
        current_milestone=milestone.value if milestone is not None else None,
        ball_with=ball_with.value if hasattr(ball_with, "value") else str(ball_with),
        project_status=status.value if hasattr(status, "value") else str(status),
        has_pending_staged_update=pending,
    )


def anchor_system_message(anchors: LoopAnchors) -> dict:
    attrs: list[str] = []
    if anchors.project_id:
        attrs.append(f'project_id="{anchors.project_id}"')
    if anchors.current_milestone:
        attrs.append(f'milestone="{anchors.current_milestone}"')
    if anchors.ball_with:
        attrs.append(f'ball_with="{anchors.ball_with}"')
    if anchors.project_status:
        attrs.append(f'status="{anchors.project_status}"')
    if anchors.has_pending_staged_update:
        attrs.append('pending_staged_update="true"')
    attr_str = " ".join(attrs)
    return {
        "role": "system",
        "content": (
            f"<loop-anchors {attr_str} />\n"
            "These values are DB truth as of this round. "
            "Do not contradict them in replies; refresh via get_project_truth if unsure."
        ),
    }