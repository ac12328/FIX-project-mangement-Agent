"""Shared policy boundary for proposed-update review actions."""

from __future__ import annotations

import json
import logging
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable

from src.db.savepoint import savepoint
from src.events.bus import Event, event_bus
from src.repositories.activity_log_repo import insert_activity
from src.repositories.project_corrections_repo import insert_correction
from src.repositories.project_repo import get_project_field
from src.services.review_validation import (
    _parse_changes, _refresh_stale_baseline, _stale_fields,
    _validate_protected_assessment,
)
from src.repositories.working_memory_repo import upsert_memory
from src.services.review_supersession import reject_superseded_email_review
from src.governance.four_eyes import (
    FourEyesError,
    assert_second_person,
    is_high_risk_project_fields,
)
from src.write_layer.write_helpers import safe_write_project
from src.write_layer.models import WriteResult
from src.project_state.observability import project_state_metrics
from src.project_reconciliation.review_resolution import resolve_reconciliation_review

logger = logging.getLogger(__name__)

_REFRESHABLE_STALE_FIELDS = frozenset({"next_action"})

@dataclass(frozen=True)
class ReviewResult:
    """Review decision result; success is not a caller commit or delivery receipt."""

    success: bool
    outcome: str
    message: str
    update_id: str
    project_id: str | None = None
    applied_fields: dict[str, Any] = field(default_factory=dict)
    stale_fields: tuple[str, ...] = ()
    write_result: WriteResult | None = None


class _ReviewClaimLost(Exception):
    """The proposal is no longer pending at the command's claim step."""


class _ReviewWriteDeclined(Exception):
    """Rollback a review claim and all partial writer effects on a soft refusal."""

    def __init__(self, result: WriteResult) -> None:
        super().__init__(result.outcome)
        self.result = result


def review_approve(
    conn: sqlite3.Connection,
    update_id: str,
    actor: str,
    surface: str,
    *,
    commit: bool = True,
    post_commit: list[Callable[[], None]] | None = None,
) -> ReviewResult:
    """Approve a proposal; defer effects when the caller owns the commit."""
    _require_review_owner(conn, commit, post_commit)
    row = _load_pending_review(conn, update_id)
    if row is None:
        return _missing_result(update_id)

    data = dict(row)
    project_id = data["project_id"]
    changes, error = _parse_changes(data.get("proposed_changes"))
    if error:
        return ReviewResult(
            success=False,
            outcome="invalid",
            message=error,
            update_id=update_id,
            project_id=project_id,
        )

    metadata_error = _validate_protected_assessment(data, changes)
    if metadata_error:
        return ReviewResult(
            success=False,
            outcome="evidence_required",
            message=metadata_error,
            update_id=update_id,
            project_id=project_id,
        )

    supersession = reject_superseded_email_review(
        conn,
        data=data,
        changes=changes,
        actor=actor,
        surface=surface,
        commit=commit,
        post_commit=post_commit,
    )
    if supersession is not None:
        return ReviewResult(
            success=False,
            outcome=supersession.outcome,
            message=supersession.message,
            update_id=supersession.update_id,
            project_id=supersession.project_id,
        )

    from src.project_reconciliation.progress_review import validate_progress_review
    progress_error = validate_progress_review(conn, data, changes)
    if progress_error:
        return ReviewResult(
            success=False, outcome="evidence_required", message=progress_error,
            update_id=update_id, project_id=project_id,
        )

    fields = {change["field"]: change.get("new_value") for change in changes}
    # Capture the version before risk/stale checks, not after claiming the review.
    current_updated = get_project_field(conn, project_id, "updated_at")
    high_risk, _risk_reason = is_high_risk_project_fields(conn, project_id, fields)
    if high_risk:
        try:
            assert_second_person(data.get("operator_id"), actor, conn=conn)
        except FourEyesError as exc:
            return ReviewResult(
                success=False,
                outcome=exc.code,
                message=exc.message,
                update_id=update_id,
                project_id=project_id,
            )

    stale_fields = _stale_fields(conn, project_id, changes)
    if stale_fields:
        if set(stale_fields).issubset(_REFRESHABLE_STALE_FIELDS):
            if not _refresh_stale_baseline(
                conn, update_id, project_id, changes, stale_fields, commit=commit,
            ):
                return _missing_result(update_id)
            fields_text = ", ".join(stale_fields)
            return ReviewResult(
                success=False,
                outcome="refreshed",
                message=(
                    "The review was refreshed against the latest project state for: "
                    f"{fields_text}. Please check the updated before/after values and "
                    "approve again."
                ),
                update_id=update_id,
                project_id=project_id,
                stale_fields=tuple(stale_fields),
            )
        project_state_metrics.increment("stale_proposal_rejection")
        fields_text = ", ".join(stale_fields)
        return ReviewResult(
            success=False,
            outcome="stale",
            message=(
                "This review is stale because the project changed after it was "
                f"created: {fields_text}. Please re-evaluate it."
            ),
            update_id=update_id,
            project_id=project_id,
            stale_fields=tuple(stale_fields),
        )

    try:
        with savepoint(conn, ensure_transaction=True):
            claim = conn.execute(
                """UPDATE proposed_updates SET decision_outcome='accepted'
                   WHERE update_id=? AND decision_outcome='needs_review'""",
                (update_id,),
            )
            if claim.rowcount != 1:
                raise _ReviewClaimLost()

            evidence_refs = [
                change.get("evidence_ref")
                for change in changes
                if change.get("evidence_ref")
            ]
            result = safe_write_project(
                conn,
                project_id,
                fields,
                actor=actor,
                operator_confirmed=True,
                evidence_ref=evidence_refs[0] if evidence_refs else data.get("source_ref"),
                reason=f"{actor} approved review {update_id} via {surface}",
                surface=surface,
                expected_updated_at=current_updated,
                commit=False,
                publish_event=False,
                schedule_notifications=False,
                rollback_on_error=False,
            )
            if result.outcome != "accepted":
                raise _ReviewWriteDeclined(result)

            now = datetime.now(timezone.utc).isoformat()
            conn.execute(
                """UPDATE proposed_updates
                   SET decision_outcome='accepted', decided_at=?, decided_by=?
                   WHERE update_id=? AND decision_outcome='accepted'""",
                (now, actor, update_id),
            )
            insert_activity(
                conn,
                actor=actor,
                action_type="review_approved",
                project_id=project_id,
                surface=surface,
                object_ref=update_id,
                summary=f"Review approved: {', '.join(result.applied_fields.keys())}",
            )
            resolve_reconciliation_review(conn, data, accepted=True, applied_fields=result.applied_fields)
            from src.email.advisory_delivery_policy import refresh_advisories_after_review
            refresh_advisories_after_review(conn, project_id=project_id,
                update_id=update_id, applied_fields=result.applied_fields)
    except _ReviewClaimLost:
        return _missing_result(update_id)
    except _ReviewWriteDeclined as declined:
        result = declined.result
        reason = result.rejection_reason or result.needs_review_reason or "no changes applied"
        return ReviewResult(
            success=False,
            outcome=result.outcome,
            message=reason,
            update_id=update_id,
            project_id=project_id,
            write_result=result,
        )
    def after_commit() -> None:
        _publish_review_event(Event(
            event_type="project_updated", project_id=project_id,
            detail={"fields": list(result.applied_fields)}, surface=surface,
        ))
        _publish_review_event(Event(
            event_type="review_approved",
            project_id=project_id,
            detail={
                "update_id": update_id,
                "applied": list(result.applied_fields.keys()),
                "surface": surface,
            },
            surface=surface,
        ))
        if result.applied_fields.get("current_milestone") == "M8_GoLive":
            try:
                from src.services.go_live_notice import schedule_go_live_telegram

                schedule_go_live_telegram(project_id)
            except Exception:
                logger.exception(
                    "Failed to schedule the committed go-live review notice for %s",
                    project_id,
                )
        project_state_metrics.record_review("approved")

    _finish_review(conn, commit=commit, post_commit=post_commit, effect=after_commit)
    return ReviewResult(
        success=True,
        outcome="accepted",
        message="Review approved",
        update_id=update_id,
        project_id=project_id,
        applied_fields=result.applied_fields,
        write_result=result,
    )


def review_reject(
    conn: sqlite3.Connection,
    update_id: str,
    reason: str,
    actor: str,
    surface: str,
    *,
    tags: list[str] | None = None,
    fallback_project_id: str | None = None,
    commit: bool = True,
    post_commit: list[Callable[[], None]] | None = None,
) -> ReviewResult:
    """Reject one pending proposal and persist structured operator feedback."""
    _require_review_owner(conn, commit, post_commit)
    row = _load_pending_review(conn, update_id)
    if row is None:
        if fallback_project_id is not None:
            return _record_unlinked_rejection(
                conn,
                update_id=update_id,
                project_id=fallback_project_id,
                reason=reason,
                tags=tags,
                actor=actor,
                surface=surface,
                commit=commit,
                post_commit=post_commit,
            )
        return _missing_result(update_id)

    data = dict(row)
    project_id = data["project_id"]
    changes, error = _parse_changes(data.get("proposed_changes"))
    if error:
        return ReviewResult(
            success=False,
            outcome="invalid",
            message=error,
            update_id=update_id,
            project_id=project_id,
        )

    try:
        with savepoint(conn, ensure_transaction=True):
            normalized_tags = list(dict.fromkeys(tag.strip() for tag in (tags or []) if tag.strip()))
            normalized_reason = reason.strip()
            rejection_payload = json.dumps(
                {"tags": normalized_tags, "reason": normalized_reason},
                ensure_ascii=False,
            )
            now = datetime.now(timezone.utc).isoformat()
            claimed = conn.execute(
                """UPDATE proposed_updates
                   SET decision_outcome='rejected', rejection_reasons=?, decided_at=?
                   WHERE update_id=? AND decision_outcome='needs_review'""",
                (rejection_payload, now, update_id),
            )
            if claimed.rowcount != 1:
                raise _ReviewClaimLost()

            for change in changes:
                insert_correction(
                    conn,
                    project_id=project_id,
                    field=change["field"],
                    wrong_value=str(change.get("new_value", "")),
                    correct_value=str(change.get("old_value", "")),
                    source="operator_rejection",
                    commit=False,
                )

            tag_text = ", ".join(normalized_tags) if normalized_tags else "no tag"
            note = f"[{tag_text}] {normalized_reason}".strip()
            upsert_memory(conn, project_id, f"review_rejected_{update_id}", note)
            insert_activity(
                conn,
                actor=actor,
                action_type="review_rejected",
                project_id=project_id,
                surface=surface,
                object_ref=update_id,
                summary=f"Review rejected. Tags: {tag_text}. Reason: {normalized_reason or 'none'}",
            )
            resolve_reconciliation_review(conn, data, accepted=False)
    except _ReviewClaimLost:
        return _missing_result(update_id)
    def after_commit() -> None:
        _publish_review_event(Event(
            event_type="review_rejected",
            project_id=project_id,
            detail={
                "update_id": update_id,
                "tags": normalized_tags,
                "reason": normalized_reason,
                "surface": surface,
            },
            surface=surface,
        ))
        project_state_metrics.record_review("rejected")

    _finish_review(conn, commit=commit, post_commit=post_commit, effect=after_commit)
    return ReviewResult(
        success=True,
        outcome="rejected",
        message="Review rejected",
        update_id=update_id,
        project_id=project_id,
    )


def _record_unlinked_rejection(
    conn: sqlite3.Connection,
    *,
    update_id: str,
    project_id: str,
    reason: str,
    tags: list[str] | None,
    actor: str,
    surface: str,
    commit: bool,
    post_commit: list[Callable[[], None]] | None,
) -> ReviewResult:
    """Preserve legacy card rejection audit when no proposal row is linked."""
    normalized_tags = list(
        dict.fromkeys(tag.strip() for tag in (tags or []) if tag.strip())
    )
    normalized_reason = reason.strip()
    tag_text = ", ".join(normalized_tags) if normalized_tags else "no tag"
    with savepoint(conn, ensure_transaction=True):
        insert_activity(
            conn,
            actor=actor,
            action_type="review_rejected",
            project_id=project_id,
            surface=surface,
            object_ref=None,
            summary=(
                f"Review rejected. Tags: {tag_text}. "
                f"Reason: {normalized_reason or 'none'}"
            ),
        )
    def after_commit() -> None:
        _publish_review_event(Event(
            event_type="review_rejected",
            project_id=project_id,
            detail={
                "update_id": None,
                "tags": normalized_tags,
                "reason": normalized_reason,
                "surface": surface,
            },
            surface=surface,
        ))

    _finish_review(conn, commit=commit, post_commit=post_commit, effect=after_commit)
    return ReviewResult(
        success=True,
        outcome="rejected",
        message="Review rejected",
        update_id=update_id,
        project_id=project_id,
    )


def _require_review_owner(
    conn: sqlite3.Connection, commit: bool,
    post_commit: list[Callable[[], None]] | None,
) -> None:
    """Refuse to take ownership of a transaction already started by the caller."""
    if commit and conn.in_transaction:
        raise ValueError("commit=True cannot own an existing transaction")
    if not commit and post_commit is None:
        raise ValueError("post_commit queue is required for caller-owned review writes")


def _finish_review(
    conn: sqlite3.Connection, *, commit: bool,
    post_commit: list[Callable[[], None]] | None,
    effect: Callable[[], None],
) -> None:
    if commit:
        conn.commit()
        try:
            effect()
        except Exception:
            logger.exception("Committed review post-commit effect failed")
    else:
        assert post_commit is not None
        post_commit.append(effect)


def _publish_review_event(event: Event) -> None:
    try:
        event_bus.publish(event)
    except Exception:
        logger.exception("Committed review event publication failed: %s", event.event_type)


def _load_pending_review(
    conn: sqlite3.Connection, update_id: str
) -> sqlite3.Row | None:
    return conn.execute(
        """SELECT * FROM proposed_updates
           WHERE update_id=? AND decision_outcome='needs_review'""",
        (update_id,),
    ).fetchone()


def _missing_result(update_id: str) -> ReviewResult:
    return ReviewResult(
        success=False,
        outcome="not_found",
        message="Review not found or already processed",
        update_id=update_id,
    )
