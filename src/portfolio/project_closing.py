"""Safe, evidence-backed project closing workflow.

Closing is deliberately a two-step lifecycle: advance to M8 with evidence, then
resolve the closing checklist and obtain an explicit Dashboard operator sign-off.
No Telegram or agent path may perform the terminal transition.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import asdict, dataclass, field
from datetime import date, datetime, timezone
from typing import Iterable

from src.events.bus import Event, event_bus
from src.models.enums import Milestone, ProjectStatus
from src.repositories.project_repo import get_project, update_project_fields


@dataclass(frozen=True)
class ChecklistDefinition:
    item_key: str
    label: str
    can_dismiss: bool = True


CHECKLIST_DEFINITIONS: tuple[ChecklistDefinition, ...] = (
    ChecklistDefinition("conformance_complete", "All applicable conformance/PCT tests passed"),
    ChecklistDefinition(
        "production_session_ready",
        "Production session parameters and readiness confirmed",
        can_dismiss=False,
    ),
    ChecklistDefinition(
        "production_authority_signoff",
        "Named authority approved production enablement or handover",
        can_dismiss=False,
    ),
    ChecklistDefinition("go_live_ownership_confirmed", "Go-live date or post-handover owner confirmed"),
    ChecklistDefinition("production_monitoring_ready", "Production monitoring and support ownership configured"),
    ChecklistDefinition("client_b2b_notified", "Client or B2B owner notified of production handover"),
    ChecklistDefinition("network_connectivity_verified", "Network connectivity and session verified"),
    ChecklistDefinition("documentation_handover_complete", "Log review and documentation handover complete"),
)

CLOSING_CHECKLIST = [definition.label for definition in CHECKLIST_DEFINITIONS]
_DEFINITION_BY_KEY = {definition.item_key: definition for definition in CHECKLIST_DEFINITIONS}
_LEGACY_KEY_BY_LABEL = {
    "All conformance tests passed": "conformance_complete",
    "Production session parameters confirmed": "production_session_ready",
    "Go-live date confirmed with counterparty": "go_live_ownership_confirmed",
    "Internal sign-off obtained": "production_authority_signoff",
    "Production monitoring configured": "production_monitoring_ready",
    "Client notified of go-live": "client_b2b_notified",
    "Network connectivity and session verified": "network_connectivity_verified",
    "Log review and documentation handover complete": "documentation_handover_complete",
}
_NON_DISMISSIBLE_KEYS = {
    definition.item_key for definition in CHECKLIST_DEFINITIONS if not definition.can_dismiss
}
_TERMINAL_STATUSES = {
    ProjectStatus.COMPLETED.value,
    ProjectStatus.ARCHIVED.value,
    ProjectStatus.CANCELLED.value,
}
_OPEN_TICKET_STATUSES = ("open", "investigating", "waiting_external", "waiting_internal")
_PENDING_UPDATE_STATUSES = ("needs_review", "deferred")


@dataclass
class ClosingIssue:
    code: str
    message: str
    count: int | None = None
    requires_acknowledgement: bool = False


@dataclass
class ChecklistItem:
    item_id: str
    item_key: str
    label: str
    status: str
    can_dismiss: bool
    dismissed_reason: str | None = None
    confirmed_at: str | None = None
    evidence_ref: str | None = None
    resolution_note: str | None = None
    resolved_by: str | None = None
    resolved_at: str | None = None
    initiated_at: str | None = None


@dataclass
class ClosingStatus:
    project_id: str
    project_status: str
    current_milestone: str | None
    project_updated_at: str | None
    target_go_live: str | None
    next_action: str | None
    items: list[ChecklistItem] = field(default_factory=list)
    initiated: bool = False
    initiated_at: str | None = None
    signed_off_by: str | None = None
    signed_off_at: str | None = None
    sign_off_note: str | None = None
    acknowledgements: list[str] = field(default_factory=list)
    closed_at: str | None = None
    can_initiate: bool = False
    can_close: bool = False
    blockers: list[ClosingIssue] = field(default_factory=list)
    warnings: list[ClosingIssue] = field(default_factory=list)
    close_blocked_reasons: list[str] = field(default_factory=list)
    checklist: dict[str, bool] = field(default_factory=dict)

    def to_dict(self) -> dict:
        return asdict(self)


class ClosingValidationError(ValueError):
    """A user-correctable closing workflow validation error."""


def _project_row(conn: sqlite3.Connection, project_id: str) -> sqlite3.Row | None:
    return conn.execute(
        """SELECT project_id, project_status, current_milestone, updated_at,
                  target_go_live, next_action
           FROM projects WHERE project_id = ?""",
        (project_id,),
    ).fetchone()


def _item_key(row: sqlite3.Row) -> str:
    key = row["item_key"] if "item_key" in row.keys() else None
    if key:
        return str(key)
    return _LEGACY_KEY_BY_LABEL.get(str(row["label"]), str(row["item_id"]))


def _definition_for(key: str, label: str) -> ChecklistDefinition:
    return _DEFINITION_BY_KEY.get(key, ChecklistDefinition(key, label, can_dismiss=True))


def _count(conn: sqlite3.Connection, sql: str, params: tuple) -> int:
    return int(conn.execute(sql, params).fetchone()[0])


def _operational_blockers(
    conn: sqlite3.Connection,
    project_id: str,
    project_status: str,
    current_milestone: str | None,
) -> list[ClosingIssue]:
    blockers: list[ClosingIssue] = []
    if project_status in _TERMINAL_STATUSES:
        blockers.append(ClosingIssue("terminal_status", f"Project is already {project_status}."))
    if current_milestone != Milestone.M8_GO_LIVE.value:
        blockers.append(
            ClosingIssue(
                "not_m8",
                f"Project must be at {Milestone.M8_GO_LIVE.value} before closing.",
            )
        )
    if project_status == ProjectStatus.BLOCKED.value:
        blockers.append(ClosingIssue("project_blocked", "Project status is Blocked."))

    followups = _count(
        conn,
        """SELECT COUNT(*) FROM internal_followups
           WHERE project_id = ? AND status IN ('open', 'overdue')""",
        (project_id,),
    )
    if followups:
        blockers.append(ClosingIssue("open_followups", "Resolve open or overdue follow-ups.", followups))

    pending_updates = _count(
        conn,
        """SELECT COUNT(*) FROM proposed_updates
           WHERE project_id = ?
             AND (decision_outcome IS NULL OR decision_outcome IN ('needs_review', 'deferred'))""",
        (project_id,),
    )
    if pending_updates:
        blockers.append(
            ClosingIssue("pending_updates", "Resolve pending project updates or reviews.", pending_updates)
        )

    placeholders = ",".join("?" for _ in _OPEN_TICKET_STATUSES)
    tickets = _count(
        conn,
        f"""SELECT COUNT(*) FROM support_tickets
             WHERE related_project_id = ? AND status IN ({placeholders})""",
        (project_id, *_OPEN_TICKET_STATUSES),
    )
    if tickets:
        blockers.append(
            ClosingIssue("open_support_tickets", "Resolve linked support tickets before closing.", tickets)
        )
    return blockers


def get_closing_status(conn: sqlite3.Connection, project_id: str) -> ClosingStatus | None:
    """Return the canonical closing read model and server-calculated readiness."""
    project = _project_row(conn, project_id)
    if project is None:
        return None

    rows = conn.execute(
        """SELECT item_id, item_key, label, status, dismissed_reason, confirmed_at,
                  initiated_at, operator_sign_off, signed_off_at, sign_off_note,
                  acknowledgements_json, closed_at, evidence_ref, resolution_note,
                  resolved_by, resolved_at
           FROM closing_checklist
           WHERE project_id = ?
           ORDER BY initiated_at, item_id""",
        (project_id,),
    ).fetchall()

    row_by_key = {_item_key(row): row for row in rows}
    items: list[ChecklistItem] = []
    for definition in CHECKLIST_DEFINITIONS:
        row = row_by_key.pop(definition.item_key, None)
        if row is None:
            items.append(
                ChecklistItem(
                    item_id="",
                    item_key=definition.item_key,
                    label=definition.label,
                    status="pending",
                    can_dismiss=definition.can_dismiss,
                )
            )
            continue
        items.append(
            ChecklistItem(
                item_id=str(row["item_id"]),
                item_key=definition.item_key,
                label=str(row["label"]),
                status=str(row["status"]),
                can_dismiss=definition.can_dismiss,
                dismissed_reason=row["dismissed_reason"],
                confirmed_at=row["confirmed_at"],
                evidence_ref=row["evidence_ref"],
                resolution_note=row["resolution_note"],
                resolved_by=row["resolved_by"],
                resolved_at=row["resolved_at"],
                initiated_at=row["initiated_at"],
            )
        )
    for key, row in row_by_key.items():
        definition = _definition_for(key, str(row["label"]))
        items.append(
            ChecklistItem(
                item_id=str(row["item_id"]),
                item_key=key,
                label=str(row["label"]),
                status=str(row["status"]),
                can_dismiss=definition.can_dismiss,
                dismissed_reason=row["dismissed_reason"],
                confirmed_at=row["confirmed_at"],
                evidence_ref=row["evidence_ref"],
                resolution_note=row["resolution_note"],
                resolved_by=row["resolved_by"],
                resolved_at=row["resolved_at"],
                initiated_at=row["initiated_at"],
            )
        )

    project_status = str(project["project_status"])
    milestone = project["current_milestone"]
    operational = _operational_blockers(conn, project_id, project_status, milestone)
    closing_blockers: list[ClosingIssue] = []
    initiated = bool(rows)
    if not initiated:
        closing_blockers.append(ClosingIssue("closing_not_initiated", "Closing checklist has not been initiated."))
    else:
        pending = [item for item in items if item.status == "pending"]
        if pending:
            closing_blockers.append(
                ClosingIssue("checklist_incomplete", "Complete every closing checklist item.", len(pending))
            )
        invalid_required = [
            item for item in items
            if item.item_key in _NON_DISMISSIBLE_KEYS and item.status != "confirmed"
        ]
        if invalid_required:
            closing_blockers.append(
                ClosingIssue(
                    "mandatory_item_not_confirmed",
                    "Production readiness and authority sign-off must be confirmed, not dismissed.",
                    len(invalid_required),
                )
            )
        missing_resolution = [
            item for item in items
            if item.status == "confirmed" and not (
                (item.evidence_ref and str(item.evidence_ref).strip())
                or (item.resolution_note and str(item.resolution_note).strip())
            )
        ]
        if missing_resolution:
            closing_blockers.append(
                ClosingIssue(
                    "missing_resolution_evidence",
                    "Confirmed checklist items require evidence or a manual attestation note.",
                    len(missing_resolution),
                )
            )

    warnings: list[ClosingIssue] = []
    target = project["target_go_live"]
    if target:
        try:
            if date.fromisoformat(str(target)[:10]) > datetime.now(timezone.utc).date():
                warnings.append(
                    ClosingIssue(
                        "future_target_go_live",
                        f"Target go-live {str(target)[:10]} is in the future; confirm this closes onboarding scope only.",
                        requires_acknowledgement=True,
                    )
                )
        except ValueError:
            pass
    next_action = project["next_action"]
    if next_action and str(next_action).strip():
        warnings.append(
            ClosingIssue(
                "residual_next_action",
                f"Current next action will be cleared: {str(next_action).strip()}",
                requires_acknowledgement=True,
            )
        )

    acknowledgement_values: list[str] = []
    raw_acknowledgements = next(
        (row["acknowledgements_json"] for row in rows if row["acknowledgements_json"]),
        None,
    )
    if raw_acknowledgements:
        try:
            parsed = json.loads(str(raw_acknowledgements))
            if isinstance(parsed, list):
                acknowledgement_values = [str(value) for value in parsed]
        except (TypeError, ValueError):
            acknowledgement_values = []

    status = ClosingStatus(
        project_id=project_id,
        project_status=project_status,
        current_milestone=milestone,
        project_updated_at=project["updated_at"],
        target_go_live=target,
        next_action=next_action,
        items=items,
        initiated=initiated,
        initiated_at=rows[0]["initiated_at"] if rows else None,
        signed_off_by=next((row["operator_sign_off"] for row in rows if row["operator_sign_off"]), None),
        signed_off_at=next((row["signed_off_at"] for row in rows if row["signed_off_at"]), None),
        sign_off_note=next((row["sign_off_note"] for row in rows if row["sign_off_note"]), None),
        acknowledgements=acknowledgement_values,
        closed_at=next((row["closed_at"] for row in rows if row["closed_at"]), None),
        blockers=operational + closing_blockers,
        warnings=warnings,
    )
    status.can_initiate = not initiated and not operational
    status.can_close = initiated and not operational and not closing_blockers
    status.close_blocked_reasons = [issue.message for issue in status.blockers]
    status.checklist = {
        item.label: item.status in ("confirmed", "dismissed") for item in items
    }
    return status


def initiate_closing(
    conn: sqlite3.Connection,
    project_id: str,
    initiated_by: str = "operator",
) -> ClosingStatus:
    """Create checklist rows after validating M8 operational readiness."""
    status = get_closing_status(conn, project_id)
    if status is None:
        raise ClosingValidationError("Project not found")
    operational = _operational_blockers(
        conn, project_id, status.project_status, status.current_milestone
    )
    if operational:
        raise ClosingValidationError(
            "Cannot initiate closing: " + "; ".join(issue.message for issue in operational)
        )

    now = datetime.now(timezone.utc).isoformat()
    for definition in CHECKLIST_DEFINITIONS:
        conn.execute(
            """INSERT OR IGNORE INTO closing_checklist
               (item_id, project_id, item_key, label, status, initiated_at, initiated_by)
               VALUES (?, ?, ?, ?, 'pending', ?, ?)""",
            (
                f"cc_{project_id}_{definition.item_key}",
                project_id,
                definition.item_key,
                definition.label,
                now,
                initiated_by,
            ),
        )
    return get_closing_status(conn, project_id)  # type: ignore[return-value]


def _validate_evidence_ref(conn: sqlite3.Connection, project_id: str, evidence_ref: str) -> None:
    row = conn.execute(
        "SELECT 1 FROM evidence_store WHERE evidence_id = ? AND project_id = ?",
        (evidence_ref, project_id),
    ).fetchone()
    if row is None:
        raise ClosingValidationError("Evidence was not found for this project.")


def confirm_checklist_item(
    conn: sqlite3.Connection,
    project_id: str,
    item_id: str,
    *,
    evidence_ref: str | None = None,
    resolution_note: str | None = None,
    manual_attestation: bool = False,
    resolved_by: str = "operator",
) -> bool:
    """Confirm an item with project evidence or an explicit manual attestation."""
    evidence_ref = (evidence_ref or "").strip() or None
    resolution_note = (resolution_note or "").strip() or None
    if evidence_ref:
        _validate_evidence_ref(conn, project_id, evidence_ref)
    elif not (manual_attestation and resolution_note):
        raise ClosingValidationError(
            "Confirmation requires project evidence or a manual attestation note."
        )

    now = datetime.now(timezone.utc).isoformat()
    cur = conn.execute(
        """UPDATE closing_checklist
           SET status = 'confirmed', confirmed_at = ?, dismissed_reason = NULL,
               evidence_ref = ?, resolution_note = ?, resolved_by = ?, resolved_at = ?
           WHERE item_id = ? AND project_id = ? AND status = 'pending'""",
        (now, evidence_ref, resolution_note, resolved_by, now, item_id, project_id),
    )
    return cur.rowcount > 0


def dismiss_checklist_item(
    conn: sqlite3.Connection,
    project_id: str,
    item_id: str,
    reason: str,
    *,
    resolved_by: str = "operator",
) -> bool:
    """Dismiss an optional item as not applicable, with a mandatory reason."""
    reason = (reason or "").strip()
    if not reason:
        raise ClosingValidationError("Dismissal reason is required.")
    row = conn.execute(
        "SELECT item_id, item_key, label FROM closing_checklist WHERE item_id = ? AND project_id = ?",
        (item_id, project_id),
    ).fetchone()
    if row is None:
        return False
    key = _item_key(row)
    if key in _NON_DISMISSIBLE_KEYS:
        raise ClosingValidationError("This safety-critical checklist item cannot be dismissed.")

    now = datetime.now(timezone.utc).isoformat()
    cur = conn.execute(
        """UPDATE closing_checklist
           SET status = 'dismissed', dismissed_reason = ?, confirmed_at = NULL,
               evidence_ref = NULL, resolution_note = ?, resolved_by = ?, resolved_at = ?
           WHERE item_id = ? AND project_id = ? AND status = 'pending'""",
        (reason, reason, resolved_by, now, item_id, project_id),
    )
    return cur.rowcount > 0


def confirm_checklist_item_by_label(
    conn: sqlite3.Connection,
    project_id: str,
    label: str,
    *,
    evidence_ref: str | None = None,
    resolution_note: str | None = None,
    manual_attestation: bool = False,
    resolved_by: str = "operator",
) -> bool:
    """Backward-compatible label helper used by legacy tests and integrations."""
    status = get_closing_status(conn, project_id)
    if status is None:
        return False
    if not status.initiated:
        initiate_closing(conn, project_id, initiated_by=resolved_by)
    row = conn.execute(
        "SELECT item_id FROM closing_checklist WHERE project_id = ? AND label = ?",
        (project_id, label),
    ).fetchone()
    if row is None:
        return False
    return confirm_checklist_item(
        conn,
        project_id,
        row["item_id"],
        evidence_ref=evidence_ref,
        resolution_note=resolution_note or "Legacy operator confirmation",
        manual_attestation=manual_attestation or not evidence_ref,
        resolved_by=resolved_by,
    )


def advance_project_to_m8(
    conn: sqlite3.Connection,
    project_id: str,
    *,
    actor: str,
    evidence_ref: str,
    expected_project_updated_at: str,
    reason: str,
    surface: str = "dashboard",
) -> tuple[bool, str]:
    """Perform the evidence-backed M7 -> M8 transition as a separate write."""
    if surface != "dashboard":
        return False, "The M8 transition is available in Dashboard only."
    project = _project_row(conn, project_id)
    if project is None:
        return False, "Project not found"
    if project["updated_at"] != expected_project_updated_at:
        return False, "Project changed after this review was loaded. Refresh and review again."
    if project["project_status"] not in (ProjectStatus.ACTIVE.value, ProjectStatus.WAITING.value):
        return False, "Only an Active or Waiting project can advance to M8 from closing review."
    if project["current_milestone"] != Milestone.M7_LOG_REVIEW_AND_PRODUCTION_SESSION_DELIVERY.value:
        return False, "Only an M7 project can use this closing-page transition."
    _validate_evidence_ref(conn, project_id, evidence_ref)

    from src.write_layer.write_helpers import safe_write_project

    result = safe_write_project(
        conn,
        project_id,
        {"current_milestone": Milestone.M8_GO_LIVE.value},
        actor=actor,
        evidence_ref=evidence_ref,
        operator_confirmed=True,
        reason=reason.strip() or "Operator confirmed production readiness and M8 transition.",
        surface=surface,
    )
    if result.outcome not in {"accepted", "no_change"}:
        return False, result.rejection_reason or result.needs_review_reason or "M8 transition failed."
    return True, f"Project {project_id} advanced to {Milestone.M8_GO_LIVE.value}."


def close_project(
    conn: sqlite3.Connection,
    project_id: str,
    *,
    actor: str = "operator",
    surface: str = "dashboard",
    expected_project_updated_at: str | None = None,
    sign_off_note: str = "",
    acknowledgements: Iterable[str] = (),
) -> tuple[bool, str]:
    """Complete an M8 project after explicit Dashboard-only final review."""
    if surface != "dashboard":
        return False, "Final project closing is available in Dashboard only."
    sign_off_note = sign_off_note.strip()
    if not sign_off_note:
        return False, "A final operator sign-off note is required."

    status = get_closing_status(conn, project_id)
    if status is None:
        return False, "Project not found"
    if not expected_project_updated_at:
        return False, "A fresh project version is required. Refresh the closing review."
    if status.project_updated_at != expected_project_updated_at:
        return False, "Project changed after this review was loaded. Refresh and review again."
    if not status.can_close:
        return False, "Cannot close: " + "; ".join(status.close_blocked_reasons)

    required_acknowledgements = {
        warning.code for warning in status.warnings if warning.requires_acknowledgement
    }
    missing = sorted(required_acknowledgements - set(acknowledgements))
    if missing:
        return False, "Missing required acknowledgement(s): " + ", ".join(missing)

    authority_item = next(
        (item for item in status.items if item.item_key == "production_authority_signoff"),
        None,
    )
    close_evidence_ref = authority_item.evidence_ref if authority_item else None

    from src.governance.four_eyes import (
        gate_high_risk_write,
        is_bound_actor,
        is_dashboard_admin,
        team_four_eyes_enabled,
    )
    from src.write_layer.write_helpers import safe_write_project

    if team_four_eyes_enabled():
        initiated = conn.execute(
            """SELECT initiated_by FROM closing_checklist
               WHERE project_id=? AND initiated_by IS NOT NULL
               ORDER BY initiated_at LIMIT 1""",
            (project_id,),
        ).fetchone()
        proposed_by = initiated["initiated_by"] if initiated else None
        close_fields = {
            "project_status": ProjectStatus.COMPLETED.value,
            "ball_with": "Internal",
            "next_action": "",
        }
        if not is_dashboard_admin(conn, actor) and not is_bound_actor(
            str(proposed_by or ""), conn=conn
        ):
            conn.execute(
                "UPDATE closing_checklist SET initiated_by=? WHERE project_id=?",
                (actor, project_id),
            )
            return False, (
                "Closing recorded by you. Another Editor or Admin must complete "
                "the terminal close."
            )
        blocked = gate_high_risk_write(
            conn, project_id, close_fields, str(proposed_by), actor
        )
        if blocked:
            return False, blocked

    try:
        result = safe_write_project(
            conn,
            project_id,
            {
                "project_status": ProjectStatus.COMPLETED.value,
                "ball_with": "Internal",
                "next_action": "",
            },
            actor=actor,
            evidence_ref=close_evidence_ref,
            operator_confirmed=True,
            reason=sign_off_note,
            surface=surface,
            commit=False,
            publish_event=False,
            closing_authorized=True,
        )
        if result.outcome != "accepted":
            conn.rollback()
            return False, result.rejection_reason or result.needs_review_reason or "Project close was not accepted."

        now = datetime.now(timezone.utc).isoformat()
        conn.execute(
            """UPDATE closing_checklist
               SET operator_sign_off = ?, signed_off_at = ?, sign_off_note = ?,
                   acknowledgements_json = ?, closed_at = ?
               WHERE project_id = ?""",
            (
                actor,
                now,
                sign_off_note,
                json.dumps(sorted(set(acknowledgements))),
                now,
                project_id,
            ),
        )
        conn.commit()
    except Exception:
        conn.rollback()
        raise

    event_bus.publish(
        Event(
            event_type="project_updated",
            project_id=project_id,
            detail={"fields": ["project_status", "ball_with", "next_action"], "closed": True},
            surface=surface,
        )
    )
    return True, f"Project {project_id} closed successfully"


def reopen_project(
    conn: sqlite3.Connection,
    project_id: str,
    reason: str,
    *,
    actor: str = "operator",
    surface: str = "dashboard",
) -> tuple[bool, str]:
    """Dedicated audited Dashboard escape hatch for reopening a completed project."""
    if surface != "dashboard":
        return False, "Project reopening is available in Dashboard only."
    reason = (reason or "").strip()
    if not reason:
        return False, "Reopen reason is required"
    project = get_project(conn, project_id)
    if project is None:
        return False, "Project not found"
    if project.project_status != ProjectStatus.COMPLETED:
        return False, f"Project is not closed (status: {project.project_status})"

    now = datetime.now(timezone.utc).isoformat()
    notes = (project.notes or "") + f"\n[REOPENED {now} by {actor}] {reason}"
    update_project_fields(
        conn,
        project_id,
        {"project_status": ProjectStatus.ACTIVE.value, "notes": notes},
    )
    conn.commit()
    event_bus.publish(
        Event(
            event_type="project_updated",
            project_id=project_id,
            detail={"fields": ["project_status", "notes"], "reopened": True},
            surface=surface,
        )
    )
    return True, f"Project {project_id} reopened: {reason}"


def _check_closing_candidates(conn: sqlite3.Connection) -> list[str]:
    """Return inactive M8 projects that are operationally ready for close review."""
    from src.utils.calendar import business_days_between

    now = datetime.now(timezone.utc)
    rows = conn.execute(
        """SELECT project_id, last_activity_at FROM projects
           WHERE project_status IN ('Active', 'Waiting')
             AND current_milestone = 'M8_GoLive'
             AND project_id NOT IN (SELECT DISTINCT project_id FROM closing_checklist)"""
    ).fetchall()
    candidates: list[str] = []
    for row in rows:
        if not row["last_activity_at"]:
            continue
        try:
            last_dt = datetime.fromisoformat(row["last_activity_at"])
            if last_dt.tzinfo is None:
                last_dt = last_dt.replace(tzinfo=timezone.utc)
        except (ValueError, TypeError):
            continue
        if business_days_between(last_dt, now) < 7:
            continue
        status = get_closing_status(conn, row["project_id"])
        if status and not _operational_blockers(
            conn, row["project_id"], status.project_status, status.current_milestone
        ):
            candidates.append(row["project_id"])
    return candidates
