"""Second-person policy for high-risk project writes and client sends."""

from __future__ import annotations

import sqlite3
from typing import Any

from config import defaults
from src.auth.rbac import ROLE_ADMIN
from src.milestone.state_machine import MILESTONE_SEQUENCE, is_revert_transition
from src.models.enums import Milestone, ProjectStatus

UNBOUND_ACTORS = frozenset({
    "",
    "operator",
    "agent",
    "system",
    "system:legacy-send",
    "email_pipeline",
    "email_proposal",
    "email_send",
})

HIGH_RISK_STATUS = frozenset(
    status.value for status in ProjectStatus.terminal_statuses()
)
VERSIONED_PROJECT_FIELDS = frozenset({
    "project_status",
    "current_milestone",
    "ball_with",
    "next_action",
})


class FourEyesError(ValueError):
    """Raised when a high-risk action lacks a distinct bound second person."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


def team_four_eyes_enabled() -> bool:
    return bool(getattr(defaults, "TEAM_FOUR_EYES_ENABLED", False))


def dashboard_user_role(conn: sqlite3.Connection | None, actor: str | None) -> str | None:
    value = (actor or "").strip()
    if conn is None or not value:
        return None
    try:
        row = conn.execute(
            "SELECT role, status FROM dashboard_users WHERE user_id=?",
            (value,),
        ).fetchone()
    except sqlite3.Error:
        return None
    if row is None or str(row["status"]) != "active":
        return None
    return str(row["role"])


def is_dashboard_admin(conn: sqlite3.Connection | None, actor: str | None) -> bool:
    return dashboard_user_role(conn, actor) == ROLE_ADMIN


def is_bound_actor(actor: str | None, *, conn: sqlite3.Connection | None = None) -> bool:
    if is_dashboard_admin(conn, actor):
        return True
    value = (actor or "").strip()
    if not value or value in UNBOUND_ACTORS:
        return False
    configured = str(getattr(defaults, "OPERATOR_ID", "") or "").strip()
    if configured and value == configured and configured == "default_operator":
        return False
    return True


def is_milestone_jump(conn: sqlite3.Connection, project_id: str, new_milestone: str) -> bool:
    """True when the proposed milestone is more than one step forward."""
    current_raw = conn.execute(
        "SELECT current_milestone FROM projects WHERE project_id=?",
        (project_id,),
    ).fetchone()
    current = current_raw[0] if current_raw else None
    if not current or not new_milestone:
        return False
    try:
        from_ms = Milestone(current)
        to_ms = Milestone(new_milestone)
    except ValueError:
        return False
    if is_revert_transition(from_ms, to_ms):
        return False
    try:
        delta = MILESTONE_SEQUENCE.index(to_ms) - MILESTONE_SEQUENCE.index(from_ms)
    except ValueError:
        return False
    return delta > 1


def is_high_risk_project_fields(
    conn: sqlite3.Connection,
    project_id: str,
    fields: dict[str, Any],
) -> tuple[bool, str | None]:
    reasons: list[str] = []
    status = fields.get("project_status")
    if status in HIGH_RISK_STATUS:
        reasons.append(f"terminal status {status}")
    milestone = fields.get("current_milestone")
    if milestone and is_milestone_jump(conn, project_id, str(milestone)):
        reasons.append("milestone jump")
    if reasons:
        return True, "; ".join(reasons)
    return False, None


def is_high_risk_send(draft: dict[str, Any] | None = None) -> bool:
    _ = draft
    return True


def gate_high_risk_write(
    conn: sqlite3.Connection,
    project_id: str,
    fields: dict[str, Any],
    proposed_by: str | None,
    actor: str | None,
) -> str | None:
    """Return a user-facing error if this high-risk write cannot proceed."""
    if not team_four_eyes_enabled():
        return None
    high, _reason = is_high_risk_project_fields(conn, project_id, fields)
    if not high:
        return None
    try:
        assert_second_person(proposed_by, actor, conn=conn)
    except FourEyesError as exc:
        return exc.message
    return None


def assert_second_person(
    proposed_by: str | None,
    actor: str | None,
    *,
    conn: sqlite3.Connection | None = None,
) -> None:
    """Reject self-approval and unbound high-risk actors when the flag is on.

    An active Dashboard Admin may approve any high-risk change, including
    one they proposed. Editors still need a second bound person.
    """
    if not team_four_eyes_enabled():
        return
    if is_dashboard_admin(conn, actor):
        return
    if not is_bound_actor(actor, conn=conn):
        raise FourEyesError("unbound", _unbound_message(conn, actor))
    proposer = (proposed_by or "").strip()
    if proposer and proposer == actor:
        raise FourEyesError(
            "self_approve",
            "Another Editor or Admin must approve this change.",
        )


def _unbound_message(conn: sqlite3.Connection | None, actor: str | None) -> str:
    if dashboard_user_role(conn, actor) is not None:
        return "A Dashboard Admin or a second Editor must approve this high-risk change."
    return (
        "Bind this Telegram account to a Dashboard Editor before approving "
        "or sending high-risk changes."
    )


def draft_proposer(draft: dict[str, Any]) -> str | None:
    return (
        draft.get("review_requested_by")
        or draft.get("owner_user_id")
        or draft.get("created_by")
    )


def prepare_client_send(
    conn: sqlite3.Connection,
    draft: dict[str, Any],
    actor: str,
) -> str | None:
    """Return a user-facing block message, or None if *actor* may send."""
    if not team_four_eyes_enabled():
        return None
    if not is_bound_actor(actor, conn=conn):
        return (
            "Bind this Telegram account to a Dashboard Editor before sending "
            "client email."
        )
    proposer = draft_proposer(draft)
    if not proposer:
        proposer = actor
        try:
            conn.execute(
                "UPDATE drafts SET review_requested_by=? WHERE draft_id=?",
                (actor, draft.get("draft_id")),
            )
        except sqlite3.Error:
            pass
    try:
        assert_second_person(proposer, actor, conn=conn)
    except FourEyesError as exc:
        others = other_active_editors(conn, actor)
        if others and (draft.get("review_state") or "not_requested") in {
            "not_requested",
            "cancelled",
        }:
            from src.email.workspace_collaboration import request_review

            try:
                request_review(
                    conn,
                    str(draft["draft_id"]),
                    requester_user_id=actor,
                    reviewer_user_id=others[0],
                )
            except Exception:
                pass
        return exc.message
    state = draft.get("review_state") or "not_requested"
    if state != "approved":
        from src.email.workspace_collaboration import ReviewStateError, approve_review

        try:
            conn.execute(
                "UPDATE drafts SET reviewer_user_id=?, review_state='requested' WHERE draft_id=?",
                (actor, draft.get("draft_id")),
            )
            approve_review(conn, str(draft["draft_id"]), reviewer_user_id=actor)
        except (ReviewStateError, PermissionError, ValueError) as exc:
            return str(exc)
    return None


def other_active_editors(
    conn: sqlite3.Connection,
    exclude_user_id: str | None,
) -> list[str]:
    rows = conn.execute(
        """
        SELECT user_id FROM dashboard_users
        WHERE status='active' AND role IN ('Editor', 'Admin')
        ORDER BY is_primary_admin DESC, display_name COLLATE NOCASE
        """
    ).fetchall()
    exclude = (exclude_user_id or "").strip()
    return [str(row["user_id"]) for row in rows if str(row["user_id"]) != exclude]
