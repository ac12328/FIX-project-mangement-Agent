"""
Write helpers — validation functions + safe_write_project().

Replaces the 6-guard pipeline (G1-G6) + WriteEngine + Committer
with simple functions and one unified write entry point.

All guard logic is preserved as validation functions.
G6 (task-type blocking) is removed — in the unified loop,
different tools do different things; no task types.
"""

import json
import logging
import sqlite3
import uuid
from datetime import datetime, timezone

from config.defaults import EVIDENCE_REQUIRED_FIELDS, WRITABLE_PROJECT_FIELDS
from src.events.bus import Event, event_bus
from src.models.enums import BallWith, Milestone, ProjectStatus
from src.milestone.state_machine import MILESTONE_SEQUENCE, is_revert_transition
from src.repositories.activity_log_repo import insert_activity
from src.repositories.evidence_repo import insert_evidence
from src.repositories.milestone_history_repo import insert_milestone_change
from src.repositories.project_repo import (
    StaleProjectWrite,
    get_project_field,
    update_project_fields,
)
from src.write_layer.closing_guards import (
    normalize_completed_fields,
    validate_closing_transition,
    validate_completed_residual_consistency,
    validate_next_action_for_live,
)
from src.write_layer.models import WriteResult

logger = logging.getLogger(__name__)

# --- Enum helpers (from G2) ---

ENUM_FIELDS: dict[str, type] = {
    "ball_with": BallWith,
    "current_milestone": Milestone,
    "project_status": ProjectStatus,
}


def _normalize_enum_value(raw: str, enum_cls: type) -> str | None:
    """
    Map LLM-produced value to canonical enum value.
    Handles: 'BallWith.INTERNAL' → 'Internal', 'INTERNAL' → 'Internal', etc.
    """
    valid_values = {e.value for e in enum_cls}
    if raw in valid_values:
        return raw

    stripped = raw.rsplit(".", 1)[-1] if "." in raw else raw
    name_map = {e.name.upper(): e.value for e in enum_cls}
    upper = stripped.upper()
    if upper in name_map:
        return name_map[upper]

    value_map = {e.value.upper(): e.value for e in enum_cls}
    if upper in value_map:
        return value_map[upper]
    return None


# --- Validation functions ---

def validate_field_names(fields: dict) -> tuple[bool, str | None]:
    """Check all keys are in WRITABLE_PROJECT_FIELDS. (Was G1.)"""
    bad = [k for k in fields if k not in WRITABLE_PROJECT_FIELDS]
    if bad:
        return False, f"Invalid field(s): {', '.join(sorted(bad))}"
    return True, None


def normalize_and_validate_enums(fields: dict) -> tuple[bool, str | None]:
    """Auto-normalize LLM enum formatting, validate values. (Was G2.)"""
    for key, value in list(fields.items()):
        enum_cls = ENUM_FIELDS.get(key)
        if enum_cls is None:
            continue
        # Allow None for nullable fields
        if value is None and key == "current_milestone":
            continue
        if value is None:
            return False, f"Field '{key}' cannot be None"
        normalized = _normalize_enum_value(str(value), enum_cls)
        if normalized is None:
            valid = sorted(e.value for e in enum_cls)
            return False, f"Invalid value '{value}' for '{key}'. Valid: {valid}"
        if normalized != value:
            logger.info("Auto-normalized %r → %r for field %s", value, normalized, key)
            fields[key] = normalized
    return True, None


def validate_milestone_transition(
    conn: sqlite3.Connection,
    project_id: str,
    new_milestone: str,
    *,
    evidence_ref: str | None = None,
) -> tuple[bool, str | None]:
    """Evidence-gated milestone jump validation.

    Rules:
    - None → M1: always valid (initial assignment)
    - Forward delta=1: always valid
    - Forward delta>1: valid only when evidence_ref is provided
    - Backward delta<0: only M7→M5 revert is allowed
    """
    current_raw = get_project_field(conn, project_id, "current_milestone")
    try:
        from_ms = Milestone(current_raw) if current_raw else None
    except ValueError:
        from_ms = None
    try:
        to_ms = Milestone(new_milestone)
    except (ValueError, TypeError):
        return False, f"Invalid milestone value '{new_milestone}'"

    # Initial assignment: None → M1
    if from_ms is None:
        if to_ms == Milestone.M1_NETWORK_SETUP:
            return True, None
        return False, f"Initial milestone must be M1_NetworkSetup, got {to_ms.value}"

    # M7→M5 revert always allowed
    if is_revert_transition(from_ms, to_ms):
        return True, None

    # Index-based delta
    try:
        current_idx = MILESTONE_SEQUENCE.index(from_ms)
        proposed_idx = MILESTONE_SEQUENCE.index(to_ms)
    except ValueError:
        return False, "Unrecognised milestone in sequence"
    delta = proposed_idx - current_idx

    if delta < 0:
        return False, (
            f"Backward milestone jump {from_ms.value}→{to_ms.value} not allowed"
        )
    if delta == 0:
        return True, None  # no-op — filtered by idempotency check, but safe here
    if delta == 1:
        return True, None
    # delta > 1: requires evidence
    if not evidence_ref:
        return False, (
            f"Milestone jump {from_ms.value}→{to_ms.value} skips {delta - 1} "
            f"step(s). Attach evidence_ref to authorize this jump."
        )
    return True, None


def validate_status_transition(
    conn: sqlite3.Connection, project_id: str, new_status: str | None = None,
) -> tuple[bool, str | None]:
    """Block writes to terminal-status projects. (Was G4 status check + engine pre-check.)"""
    current_raw = get_project_field(conn, project_id, "project_status")
    if current_raw and current_raw in {s.value for s in ProjectStatus.terminal_statuses()}:
        return False, f"Project is {current_raw} — read-only"
    return True, None


def check_evidence_required(
    conn: sqlite3.Connection, project_id: str,
    fields: dict, evidence_ref: str | None,
) -> tuple[bool, str | None]:
    """High-impact fields need evidence_ref. M7→M5 revert waived. (Was G3.)"""
    needs_evidence = [k for k in fields if k in EVIDENCE_REQUIRED_FIELDS]
    if not needs_evidence:
        return True, None

    # M7→M5 revert waives evidence for milestone
    if "current_milestone" in fields and fields["current_milestone"]:
        current_raw = get_project_field(conn, project_id, "current_milestone")
        if current_raw:
            try:
                from_ms = Milestone(current_raw)
                to_ms = Milestone(fields["current_milestone"])
                if is_revert_transition(from_ms, to_ms):
                    needs_evidence = [k for k in needs_evidence if k != "current_milestone"]
            except ValueError:
                pass

    if needs_evidence and not evidence_ref:
        return False, f"Field(s) {needs_evidence} require evidence_ref"
    return True, None


def is_high_stakes(
    conn: sqlite3.Connection, project_id: str, fields: dict,
) -> tuple[bool, str | None]:
    """
    Deterministic check: does this write need operator review?
    Replaces confidence-based G5 auto-escalation.
    """
    reasons = []

    # Terminal status changes
    if "project_status" in fields and fields["project_status"]:
        try:
            new_status = ProjectStatus(fields["project_status"])
            if new_status in ProjectStatus.terminal_statuses():
                reasons.append(f"Terminal status change → {new_status.value}")
        except ValueError:
            pass

    # M7→M5 revert or a forward jump greater than one stage
    if "current_milestone" in fields and fields["current_milestone"]:
        current_raw = get_project_field(conn, project_id, "current_milestone")
        if current_raw:
            try:
                from_ms = Milestone(current_raw)
                to_ms = Milestone(fields["current_milestone"])
                if is_revert_transition(from_ms, to_ms):
                    reasons.append("M7 → M5 revert (FAIL case)")
                else:
                    from src.governance.four_eyes import is_milestone_jump
                    if is_milestone_jump(conn, project_id, str(fields["current_milestone"])):
                        reasons.append("Milestone jump greater than +1")
            except ValueError:
                pass

    if reasons:
        return True, "; ".join(reasons)
    return False, None


def fields_unchanged(
    conn: sqlite3.Connection, project_id: str, fields: dict,
) -> dict:
    """Return canonical field values that actually differ from current state."""
    normalized_fields = dict(fields)
    for key, value in fields.items():
        enum_cls = ENUM_FIELDS.get(key)
        if enum_cls is None or value is None:
            continue
        normalized = _normalize_enum_value(str(value), enum_cls)
        if normalized is not None:
            normalized_fields[key] = normalized

    changed = {}
    for key, new_value in normalized_fields.items():
        current = get_project_field(conn, project_id, key)
        current_str = str(current) if current is not None else None
        new_str = str(new_value) if new_value is not None else None
        if current_str != new_str:
            changed[key] = new_value
    return changed


# --- Main write entry point ---

def safe_write_project(
    conn: sqlite3.Connection,
    project_id: str,
    fields: dict,
    *,
    actor: str = "agent",
    evidence_ref: str | None = None,
    operator_confirmed: bool = False,
    reason: str = "",
    surface: str = "telegram",
    commit: bool = True,
    publish_event: bool = True,
    closing_authorized: bool = False,
    completion_approval_authorized: bool = False,
    expected_updated_at: str | None = None,
    schedule_notifications: bool = True,
    rollback_on_error: bool = True,
) -> WriteResult:
    """
    Validate and write project fields atomically.
    Replaces WriteEngine.process() + 6 guards + Committer.

    Transaction owners may set schedule_notifications=False and schedule external
    notices only after their outer commit. Notice records remain transactional.
    Set rollback_on_error=False only when the caller owns a savepoint/transaction
    and rolls back partial writes itself. The compatibility default rolls back.

    Returns WriteResult with outcome:
      - "accepted": fields written to DB
      - "rejected": validation failed
      - "needs_review": high-stakes change needs operator confirmation
      - "no_change": all fields are idempotent (no-ops)
    """
    # 0. Reject unknown projects before audit/activity writes can hit FK errors.
    project_exists = conn.execute(
        "SELECT updated_at FROM projects WHERE project_id = ?", (project_id,)
    ).fetchone()
    if not project_exists:
        return WriteResult(
            outcome="rejected",
            rejection_reason=f"Project {project_id} not found.",
        )

    # An explicit version is a precondition, even for notes or idempotent fields.
    # Reject it before review/audit side effects; retain the SQL CAS for races.
    if expected_updated_at is not None and project_exists[0] != expected_updated_at:
        return WriteResult(
            outcome="rejected",
            rejection_reason="This project changed after you loaded it. Please re-evaluate.",
        )

    # 0b. Check project not in terminal status
    ok, err = validate_status_transition(conn, project_id)
    if not ok:
        return WriteResult(outcome="rejected", rejection_reason=err)

    # 1. Field name validation (was G1)
    ok, err = validate_field_names(fields)
    if not ok:
        return WriteResult(outcome="rejected", rejection_reason=err)

    # 2. Enum normalization + validation (was G2)
    ok, err = normalize_and_validate_enums(fields)
    if not ok:
        return WriteResult(outcome="rejected", rejection_reason=err)

    # 3. Milestone transition validation (was G4)
    if "current_milestone" in fields and fields["current_milestone"] is not None:
        ok, err = validate_milestone_transition(
                conn, project_id, fields["current_milestone"], evidence_ref=evidence_ref
            )
        if not ok:
            return WriteResult(outcome="rejected", rejection_reason=err)

    # 3b. Closing quality (P1) + next_action quality (P2) — before auto-clear
    ok, err = validate_closing_transition(
        conn, project_id, fields,
        operator_confirmed=operator_confirmed,
        actor=actor,
        surface=surface,
        closing_authorized=closing_authorized,
        completion_approval_authorized=completion_approval_authorized,
    )
    if not ok:
        return WriteResult(outcome="rejected", rejection_reason=err)
    ok, err = validate_next_action_for_live(
        conn, project_id, fields,
        operator_confirmed=operator_confirmed, actor=actor,
    )
    if not ok:
        return WriteResult(outcome="rejected", rejection_reason=err)
    ok, err = validate_completed_residual_consistency(
        conn, project_id, fields,
        operator_confirmed=operator_confirmed, actor=actor,
    )
    if not ok:
        return WriteResult(outcome="rejected", rejection_reason=err)

    # 3c. After guards pass: completing auto-clears residual work if still omitted
    fields = normalize_completed_fields(conn, project_id, fields)

    # 4. Evidence requirement (was G3) — operator confirmation or operator actor waives evidence
    if not operator_confirmed and actor != "operator":
        ok, err = check_evidence_required(conn, project_id, fields, evidence_ref)
        if not ok:
            return WriteResult(outcome="rejected", rejection_reason=err)

    # 5. Filter no-ops (was idempotency)
    changed = fields_unchanged(conn, project_id, fields)
    if not changed:
        return WriteResult(outcome="no_change")

    # 6. High-stakes review (replaces G5 confidence-based escalation)
    from src.project_state.pct_authorization import permits_pct_completion
    if not operator_confirmed and not permits_pct_completion(conn, project_id, fields):
        high, review_reason = is_high_stakes(conn, project_id, changed)
        if high:
            _store_audit_record(conn, project_id, changed, "needs_review", actor, reason, evidence_ref)
            if commit:
                conn.commit()
            if publish_event:
                if not commit:
                    raise ValueError("publish_event=True requires commit=True")
                event_bus.publish(Event(
                    event_type="review_queued",
                    project_id=project_id,
                    detail={"reason": review_reason},
                    surface=surface,
                ))
            return WriteResult(
                outcome="needs_review",
                applied_fields=changed,
                needs_review_reason=review_reason,
            )

    # 7. Write — atomic transaction
    reached_m8 = False
    try:
        # Capture from_milestone BEFORE write
        from_milestone = None
        if "current_milestone" in changed:
            from_milestone = get_project_field(conn, project_id, "current_milestone")

        # Capture pre-write snapshot for audit (must happen before update_project_fields)
        _snap_row = conn.execute(
            "SELECT * FROM projects WHERE project_id = ?", (project_id,)
        ).fetchone()
        pre_write_snapshot = dict(_snap_row) if _snap_row else {}

        # Snapshot the caller-supplied fields before update_project_fields injects updated_at
        audit_fields = dict(changed)

        # The ONLY call to update_project_fields
        update_project_fields(
            conn,
            project_id,
            changed,
            expected_updated_at=expected_updated_at,
        )

        # Milestone history
        if "current_milestone" in changed:
            insert_milestone_change(
                conn,
                project_id=project_id,
                from_milestone=from_milestone,
                to_milestone=changed["current_milestone"],
                changed_by=actor,
                reason=reason,
                evidence_ref=evidence_ref,
            )

        # Activity log
        summary_parts = [f"{k}={v}" for k, v in changed.items()]
        insert_activity(
            conn,
            actor=actor,
            action_type="truth_write",
            surface=surface,
            project_id=project_id,
            summary=f"Updated: {', '.join(summary_parts)}",
            metadata_json=json.dumps({"reason": reason}) if reason else None,
        )

        # Audit trail
        _store_audit_record(
            conn, project_id, audit_fields, "accepted", actor, reason, evidence_ref,
            pre_write_snapshot=pre_write_snapshot,
        )

        # Auto-create evidence record for state-field changes
        _STATE_FIELDS = {"current_milestone", "ball_with", "project_status"}
        changed_state_fields = _STATE_FIELDS & set(changed.keys())
        if changed_state_fields:
            summary_parts = [
                f"{f}: {changed[f]}" for f in sorted(changed_state_fields)
            ]
            insert_evidence(
                conn,
                project_id=project_id,
                evidence_type="fact",
                content_summary=f"State updated — {', '.join(summary_parts)} (actor: {actor})",
                evidence_at=datetime.now(timezone.utc).isoformat(),
                source_ref=evidence_ref or "system",
            )

        reached_m8 = (
            "current_milestone" in changed
            and str(changed["current_milestone"]) == Milestone.M8_GO_LIVE.value
        )
        if reached_m8:
            from src.services.go_live_notice import record_go_live_notice

            # The milestone, chat notice and configured delivery intent are one
            # transaction. A failed enqueue must roll back the business write.
            record_go_live_notice(conn, project_id, actor=actor, surface=surface)

        if commit:
            conn.commit()

        if reached_m8 and schedule_notifications and commit:
            try:
                from src.services.go_live_notice import schedule_go_live_telegram

                schedule_go_live_telegram(project_id)
            except Exception:
                logger.exception(
                    "Failed to schedule M8 go-live Telegram notice for %s",
                    project_id,
                )

        # Publish only after this helper owns the commit. Transactional callers
        # publish after their wider unit of work commits successfully.
        if publish_event:
            if not commit:
                raise ValueError("publish_event=True requires commit=True")
            event_bus.publish(Event(
                event_type="project_updated",
                project_id=project_id,
                detail={"fields": list(changed.keys())},
                surface=surface,
            ))

        return WriteResult(outcome="accepted", applied_fields=changed)

    except StaleProjectWrite as exc:
        if rollback_on_error:
            conn.rollback()
        return WriteResult(outcome="rejected", rejection_reason=str(exc))
    except Exception:
        if rollback_on_error:
            conn.rollback()
        raise


def _store_audit_record(
    conn: sqlite3.Connection,
    project_id: str,
    fields: dict,
    outcome: str,
    actor: str,
    reason: str,
    evidence_ref: str | None = None,
    *,
    pre_write_snapshot: dict | None = None,
) -> None:
    """Store a record in proposed_updates for audit trail compatibility.

    If pre_write_snapshot is provided (dict of project row before the write),
    it is used as the source of old_value. Otherwise a SELECT is issued, which
    is correct for paths where no write has happened yet (e.g. needs_review).
    """
    now = datetime.now(timezone.utc).isoformat()
    update_id = str(uuid.uuid4())

    if pre_write_snapshot is not None:
        proj = pre_write_snapshot
    else:
        proj_row = conn.execute(
            "SELECT * FROM projects WHERE project_id = ?", (project_id,)
        ).fetchone()
        proj = dict(proj_row) if proj_row else {}

    changes_json = json.dumps([
        {
            "field": k,
            "old_value": str(proj[k]) if proj.get(k) is not None else "",
            "new_value": str(v) if v is not None else "",
            "reason": reason,
            "evidence_ref": evidence_ref,
        }
        for k, v in fields.items()
    ])
    conn.execute(
        """INSERT INTO proposed_updates
           (update_id, project_id, proposed_changes, task_type,
            confidence, risk_level, requires_operator_review,
            decision_outcome, rejection_reasons, operator_id, decided_at, created_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            update_id,
            project_id,
            changes_json,
            "direct_write",
            1.0,  # deterministic — no confidence needed
            "low",
            0,
            outcome,
            None,
            actor,
            now,
            now,
        ),
    )
