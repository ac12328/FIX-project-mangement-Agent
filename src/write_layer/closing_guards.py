"""
Closing / next_action quality guards for safe_write_project.

P1: Prevent premature Completed (non-M8, residual open work).
P2: Keep next_action specific for live projects; clear on true close.
"""
from __future__ import annotations

import re
import sqlite3

from src.models.enums import Milestone, ProjectStatus
from src.repositories.project_repo import get_project_field

# Vague next_action patterns (agent must be more specific)
_VAGUE_NEXT_ACTION = re.compile(
    r"^\s*("
    r"follow\s*up|跟進一下|跟進|check|update|pending|tbd|n/?a|todo|"
    r"monitor|監視|觀察|稍後處理|稍後跟進"
    r")\.?\s*$",
    re.IGNORECASE,
)

_LIVE_STATUSES = frozenset({
    ProjectStatus.ACTIVE.value,
    ProjectStatus.BLOCKED.value,
    ProjectStatus.WAITING.value,
})


def effective_milestone(
    conn: sqlite3.Connection,
    project_id: str,
    fields: dict,
) -> str | None:
    if "current_milestone" in fields and fields["current_milestone"]:
        return str(fields["current_milestone"])
    return get_project_field(conn, project_id, "current_milestone")


def effective_status(
    conn: sqlite3.Connection,
    project_id: str,
    fields: dict,
) -> str | None:
    if "project_status" in fields and fields["project_status"]:
        return str(fields["project_status"])
    return get_project_field(conn, project_id, "project_status")


def effective_next_action(
    conn: sqlite3.Connection,
    project_id: str,
    fields: dict,
) -> str | None:
    if "next_action" in fields:
        val = fields["next_action"]
        return "" if val is None else str(val)
    current = get_project_field(conn, project_id, "next_action")
    return "" if current is None else str(current)


def is_vague_next_action(text: str | None) -> bool:
    if text is None:
        return True
    s = text.strip()
    if not s:
        return True
    if len(s) < 12:
        return True
    return bool(_VAGUE_NEXT_ACTION.match(s))


def validate_closing_transition(
    conn: sqlite3.Connection,
    project_id: str,
    fields: dict,
    *,
    operator_confirmed: bool = False,
    actor: str = "agent",
    surface: str = "telegram",
    closing_authorized: bool = False,
    completion_approval_authorized: bool = False,
) -> tuple[bool, str | None]:
    """When writing project_status=Completed, enforce terminal quality.

    Rules:
    - Milestone must be M8_GoLive (always — even operator confirm).
    - next_action must be empty / cleared in same write for agent path.
      Operator/confirm path may omit clear; normalize_completed_fields will clear.
    """
    new_status = fields.get("project_status")
    if not new_status:
        return True, None
    try:
        status_enum = ProjectStatus(str(new_status))
    except ValueError:
        return True, None  # enum validator handles invalids
    if status_enum != ProjectStatus.COMPLETED:
        return True, None
    open_followups = conn.execute(
        "SELECT COUNT(*) FROM internal_followups "
        "WHERE project_id = ? AND status IN ('open','overdue')",
        (project_id,),
    ).fetchone()[0]
    if open_followups:
        return False, (
            "Cannot set Completed while open onboarding follow-ups remain; "
            "resolve them or transfer residual work to linked support tickets first."
        )

    from src.project_state.pct_authorization import permits_pct_completion
    if permits_pct_completion(conn, project_id, fields):
        return True, None
    if surface != "dashboard":
        return False, "Final project closing is available in Dashboard only."
    if not (closing_authorized or completion_approval_authorized):
        return False, "Use the Approval Center to confirm project completion."

    ms = effective_milestone(conn, project_id, fields)
    if ms != Milestone.M8_GO_LIVE.value:
        return False, (
            f"Cannot set Completed while milestone is {ms or 'unset'}; "
            f"require {Milestone.M8_GO_LIVE.value}."
        )

    # Approval Center completion may skip the checklist; the legacy /closing route must not.
    if not completion_approval_authorized:
        from src.portfolio.project_closing import get_closing_status

        closing = get_closing_status(conn, project_id)
        if closing is None or not closing.can_close:
            reasons = closing.close_blocked_reasons if closing else ["closing checklist unavailable"]
            return False, "Cannot set Completed: " + "; ".join(reasons[:3])

    na = effective_next_action(conn, project_id, fields)
    if na.strip() and not (operator_confirmed or actor == "operator"):
        # Operator-confirmed completion is normalized to clear residual fields,
        # but only after the non-bypassable milestone/checklist checks above.
        if "next_action" not in fields or str(fields.get("next_action") or "").strip():
            return False, (
                "Cannot set Completed while next_action is non-empty; "
                "clear next_action in the same write."
            )
    return True, None


def validate_next_action_for_live(
    conn: sqlite3.Connection,
    project_id: str,
    fields: dict,
    *,
    operator_confirmed: bool = False,
    actor: str = "agent",
) -> tuple[bool, str | None]:
    """Reject empty/vague next_action writes for live projects (agent path)."""
    if "next_action" not in fields:
        return True, None
    if operator_confirmed or actor == "operator":
        return True, None

    status = effective_status(conn, project_id, fields)
    if status not in _LIVE_STATUSES:
        # Terminal / completed: empty next_action is OK
        return True, None

    raw = fields.get("next_action")
    text = "" if raw is None else str(raw)
    if not text.strip():
        return False, (
            "next_action cannot be empty for Active/Blocked/Waiting projects; "
            "set a specific executable next step."
        )
    if is_vague_next_action(text):
        return False, (
            f"next_action is too vague ({text!r}); "
            "name who/what (e.g. 'Send UAT CompIDs to client IT')."
        )
    return True, None


def normalize_completed_fields(
    conn: sqlite3.Connection,
    project_id: str,
    fields: dict,
) -> dict:
    """When completing, auto-clear residual work fields if not explicitly set.

    Mutates a copy: next_action → '' ; ball_with → Internal if omitted.
    """
    out = dict(fields)
    new_status = out.get("project_status")
    if not new_status:
        return out
    try:
        if ProjectStatus(str(new_status)) != ProjectStatus.COMPLETED:
            return out
    except ValueError:
        return out

    if "next_action" not in out:
        out["next_action"] = ""
    if "ball_with" not in out:
        out["ball_with"] = "Internal"
    return out


def validate_completed_residual_consistency(
    conn: sqlite3.Connection,
    project_id: str,
    fields: dict,
    *,
    operator_confirmed: bool = False,
    actor: str = "agent",
) -> tuple[bool, str | None]:
    """Block setting open residual next_action while already/ becoming Completed."""
    status = effective_status(conn, project_id, fields)
    if status != ProjectStatus.COMPLETED.value:
        return True, None
    if "next_action" not in fields:
        return True, None
    text = "" if fields.get("next_action") is None else str(fields["next_action"])
    if text.strip():
        return False, (
            "Completed projects must not carry open next_action work; "
            "clear next_action or set status Active/Blocked first."
        )
    return True, None
