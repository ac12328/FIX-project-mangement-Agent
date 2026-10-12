"""Auto-apply low-risk email proposals with update-scoped operator undo."""

from __future__ import annotations

import json
import logging
import math
import sqlite3
from collections.abc import Callable
from datetime import datetime, timezone

from config.settings import CONFIG
from src.repositories.project_repo import get_project_field
from src.repositories.working_memory_repo import get_memory, upsert_memory
from src.write_layer.write_helpers import safe_write_project

logger = logging.getLogger(__name__)

_PERFECT_CONFIDENCE_EPS = 1e-6
_UNDO_KEY_PREFIX = "undo_auto_apply:"
_AUTO_APPLY_FIELDS = frozenset({"next_action", "target_go_live", "sla_limit", "project_tier"})


def is_perfect_confidence(confidence: float) -> bool:
    """True when a finite confidence is effectively 1.00 and auto-apply is enabled."""
    wl = CONFIG.write_layer
    return (
        getattr(wl, "auto_apply_at_perfect_confidence", True)
        and wl.auto_write_requires_confirmation
        and math.isfinite(confidence)
        and 0.0 <= confidence <= 1.0
        and confidence >= 1.0 - _PERFECT_CONFIDENCE_EPS
    )


def can_auto_apply(proposed_fields: dict) -> bool:
    """Return whether every proposed field is in the low-risk auto-apply allowlist."""
    return bool(proposed_fields) and set(proposed_fields).issubset(_AUTO_APPLY_FIELDS)


def _undo_key(update_id: str) -> str:
    return f"{_UNDO_KEY_PREFIX}{update_id}"


def snapshot_undo_state(
    conn: sqlite3.Connection,
    project_id: str,
    update_id: str,
    proposed_fields: dict,
) -> dict:
    """Persist old and expected-new values for one auto-applied update."""
    snapshot = {
        field: {
            "old_value": get_project_field(conn, project_id, field),
            "new_value": value,
        }
        for field, value in proposed_fields.items()
    }
    upsert_memory(conn, project_id, _undo_key(update_id), json.dumps(snapshot))
    return snapshot


def revert_auto_apply(
    conn: sqlite3.Connection,
    update_id: str,
    *,
    reason: str,
    post_commit: list[Callable[[], None]] | None = None,
) -> dict:
    """Revert one auto-apply without committing a caller-owned transaction.

    A borrowed caller must dispatch ``post_commit`` only after its own commit
    and writer-lease release. Without a collector, no refresh is published.
    """
    row = conn.execute(
        "SELECT project_id, decision_outcome FROM proposed_updates WHERE update_id = ?",
        (update_id,),
    ).fetchone()
    if not row or row["decision_outcome"] != "accepted":
        return {"ok": False, "error": "undo_not_available"}

    project_id = row["project_id"]
    mem = get_memory(conn, project_id, _undo_key(update_id))
    if not mem or not mem.get("memory_value"):
        return {"ok": False, "error": "no_undo_snapshot"}

    try:
        snapshot = json.loads(mem["memory_value"])
    except (json.JSONDecodeError, TypeError):
        return {"ok": False, "error": "invalid_undo_snapshot"}
    if not isinstance(snapshot, dict) or not snapshot:
        return {"ok": False, "error": "invalid_undo_snapshot"}

    old_values: dict = {}
    for field, values in snapshot.items():
        if not isinstance(values, dict) or "old_value" not in values or "new_value" not in values:
            return {"ok": False, "error": "invalid_undo_snapshot"}
        current = get_project_field(conn, project_id, field)
        expected = values["new_value"]
        current_str = str(current) if current is not None else None
        expected_str = str(expected) if expected is not None else None
        if current_str != expected_str:
            return {"ok": False, "error": "project_changed_since_auto_apply"}
        old_values[field] = values["old_value"]

    owns_transaction = not conn.in_transaction
    if owns_transaction:
        conn.execute("BEGIN")
    conn.execute("SAVEPOINT email_auto_revert")
    savepoint_open = True
    try:
        wr = safe_write_project(
            conn,
            project_id,
            old_values,
            actor="operator",
            reason=reason,
            operator_confirmed=True,
            surface="telegram",
            commit=False,
            publish_event=False,
            schedule_notifications=False,
            rollback_on_error=False,
        )
        if wr.outcome not in {"accepted", "no_change"}:
            conn.execute("ROLLBACK TO SAVEPOINT email_auto_revert")
            conn.execute("RELEASE SAVEPOINT email_auto_revert")
            savepoint_open = False
            if owns_transaction:
                conn.rollback()
            return {"ok": False, "error": wr.rejection_reason or "revert_failed"}

        decided_at = datetime.now(timezone.utc).isoformat()
        updated = conn.execute(
            "UPDATE proposed_updates SET decision_outcome='rejected', decided_at=? "
            "WHERE update_id=? AND project_id=? AND decision_outcome='accepted'",
            (decided_at, update_id, project_id),
        )
        if updated.rowcount != 1:
            conn.execute("ROLLBACK TO SAVEPOINT email_auto_revert")
            conn.execute("RELEASE SAVEPOINT email_auto_revert")
            savepoint_open = False
            if owns_transaction:
                conn.rollback()
            return {"ok": False, "error": "undo_not_available"}
        upsert_memory(conn, project_id, _undo_key(update_id), None)
        conn.execute("RELEASE SAVEPOINT email_auto_revert")
        savepoint_open = False
        if owns_transaction:
            conn.commit()
    except Exception:
        if savepoint_open:
            conn.execute("ROLLBACK TO SAVEPOINT email_auto_revert")
            conn.execute("RELEASE SAVEPOINT email_auto_revert")
        # Only this command's transaction may be rolled back in full.
        if owns_transaction:
            conn.rollback()
        raise

    if wr.outcome == "accepted":
        def _publish_revert() -> None:
            try:
                from src.events.bus import Event, event_bus
                event_bus.publish(Event(event_type="project_updated", project_id=project_id,
                                        detail={"fields": list(old_values)}, surface="telegram"))
            except Exception:
                logger.exception("Post-commit undo event failed for %s", update_id)

        if owns_transaction:
            _publish_revert()
        elif post_commit is not None:
            post_commit.append(_publish_revert)

    revert_text = ", ".join(f"{k} → {v}" for k, v in old_values.items())
    return {
        "ok": True,
        "project_id": project_id,
        "reverted": old_values,
        "revert_text": revert_text,
        "update_id": update_id,
    }


def auto_apply_perfect_with_undo_card(
    conn: sqlite3.Connection,
    *,
    project_id: str,
    proposed_fields: dict,
    confidence: float,
    evidence_ref: str,
    actor: str,
    reason: str,
    summary: str = "",
    suggested_action: str = "",
    sender_email: str = "",
    post_commit: list[Callable[[], None]] | None = None,
) -> str:
    """Apply a low-risk proposal; deliver its undo card only after commit."""
    from src.models.enums import RiskLevel, TaskType, WriteDecisionOutcome
    from src.models.proposed_update import ProposedChange, ProposedUpdate, WriteDecision
    from src.repositories.activity_log_repo import insert_activity
    from src.repositories.proposed_update_repo import store_proposed_update
    from src.telegram.review_push import push_auto_applied_undo_card_sync

    if not can_auto_apply(proposed_fields):
        return "review_card"
    if not is_perfect_confidence(confidence):
        return "review_card"

    changes = [
        ProposedChange(
            field=field,
            new_value=str(value) if value is not None else None,
            reason=reason or f"Auto-applied from email ({sender_email})",
            evidence_ref=evidence_ref,
        )
        for field, value in proposed_fields.items()
    ]
    update = ProposedUpdate(
        project_id=project_id,
        proposed_changes=changes,
        task_type=TaskType.TRUTH_WRITE_REASONING,
        confidence=confidence,
        risk_level=RiskLevel.MEDIUM,
        requires_operator_review=False,
    )
    from src.email.update_notification_outbox import (
        enqueue_update_notification, deliver_update_notification,
    )
    from src.email.update_outcomes import (
        AUTO_APPLIED_NOTIFICATION_PENDING, AUTO_APPLIED_NOTIFICATION_UNKNOWN,
    )

    # Project truth, accepted receipt, undo snapshot and outbox are one unit.
    # Limit failure cleanup to this command; do not roll back unrelated caller work.
    owns_transaction = not conn.in_transaction
    if owns_transaction:
        conn.execute("BEGIN")
    conn.execute("SAVEPOINT email_auto_apply_update")
    savepoint_open = True
    try:
        snapshot = snapshot_undo_state(conn, project_id, update.update_id, proposed_fields)
        write_result = safe_write_project(
            conn, project_id, proposed_fields, actor=actor, reason=reason,
            evidence_ref=evidence_ref, operator_confirmed=False, surface="email",
            commit=False, publish_event=False, schedule_notifications=False,
            rollback_on_error=False,
        )
        if write_result.outcome != "accepted":
            conn.execute("ROLLBACK TO SAVEPOINT email_auto_apply_update")
            conn.execute("RELEASE SAVEPOINT email_auto_apply_update")
            savepoint_open = False
            if owns_transaction:
                conn.rollback()
            return "review_card" if write_result.outcome == "needs_review" else "skipped"
        decision = WriteDecision(
            update_id=update.update_id, project_id=project_id,
            outcome=WriteDecisionOutcome.ACCEPTED,
        )
        store_proposed_update(conn, update, decision)
        fields_desc = ", ".join(f"{k}={v}" for k, v in (write_result.applied_fields or {}).items())
        insert_activity(
            conn, actor=actor, action_type="auto_truth_write", project_id=project_id,
            surface="email", summary=f"Auto-applied at conf={confidence:.2f}: {fields_desc} (undo available)",
            object_ref=update.update_id,
        )
        from src.repositories.project_repo import get_project
        project = get_project(conn, project_id)
        enqueue_update_notification(conn, update_id=update.update_id, project_id=project_id, payload={
            "project_id": project_id, "update_id": update.update_id,
            "summary": summary or suggested_action or "Email update applied",
            "reason": f"From {sender_email}" if sender_email else reason,
            "risk_level": "MEDIUM", "evidence_refs": [evidence_ref] if evidence_ref else None,
            "proposed_changes": [{"field": field, "old_value": snapshot[field]["old_value"],
                "new_value": str(value) if value is not None else None} for field, value in proposed_fields.items()],
            "applied_fields": list((write_result.applied_fields or {}).keys()),
            "sender_email": sender_email or None,
            "client_name": project.client_name if project else None,
            "confidence": confidence, "suggested_action": suggested_action or "",
        })
        conn.execute("RELEASE SAVEPOINT email_auto_apply_update")
        savepoint_open = False
        if owns_transaction:
            conn.commit()
    except Exception:
        if savepoint_open:
            conn.execute("ROLLBACK TO SAVEPOINT email_auto_apply_update")
            conn.execute("RELEASE SAVEPOINT email_auto_apply_update")
            if owns_transaction:
                conn.rollback()
        else:
            # Only an owned commit failure may clear the entire transaction.
            if owns_transaction:
                conn.rollback()
        raise

    def _publish_update() -> None:
        try:
            from src.events.bus import Event, event_bus
            event_bus.publish(Event(event_type="project_updated", project_id=project_id,
                detail={"fields": list((write_result.applied_fields or {}).keys())}, surface="email"))
        except Exception:
            logger.exception("Post-commit project event failed for %s", update.update_id)

    if not owns_transaction:
        # The outbox worker will deliver after commit even without a callback.
        # A caller may additionally request prompt delivery after releasing its
        # write scope. Never send or report delivery while SQL can still roll back.
        if post_commit is not None:
            def _after_commit() -> None:
                _publish_update()
                try:
                    deliver_update_notification(update.update_id)
                except Exception:
                    logger.exception("Post-commit notification dispatch failed for %s", update.update_id)
            post_commit.append(_after_commit)
        return AUTO_APPLIED_NOTIFICATION_PENDING

    _publish_update()
    try:
        return deliver_update_notification(update.update_id, conn=conn,
            sender=push_auto_applied_undo_card_sync)
    except Exception:
        # The durable outbox survives. Never misreport this committed update as skipped.
        logger.exception("Notification dispatch failed for committed update %s", update.update_id)
        return AUTO_APPLIED_NOTIFICATION_UNKNOWN
