"""Persistence and review-gated writes for analyzed inbound project email.

The caller owns the write scope. This service keeps the existing evidence and
review policy sequence. Evidence remains in the caller transaction until the
review proposal and delivery intent can commit together.
"""
from __future__ import annotations

from collections.abc import Callable
from typing import Any

from pydantic import BaseModel, ConfigDict

from src.email.inbound_field_effect_identity import (
    FIELD_EFFECT_OUTCOMES, ProjectFieldEffectCommandReceipt, validate_field_effect_command,
)
from src.email.inbound_work import (
    FIELD_REVIEW_OUTCOMES, PctCompletionCommandReceipt, ProjectFieldReviewCommandReceipt,
)


class ProjectAnalysisWriteResult(BaseModel):
    model_config = ConfigDict(frozen=True)

    write_action: str
    baseline_fields: dict[str, str]
    stored: dict[str, Any]
    pct_command: PctCompletionCommandReceipt | None = None
    field_effect_command: ProjectFieldEffectCommandReceipt | None = None
    field_review_command: ProjectFieldReviewCommandReceipt | None = None


class _NonpersistentFieldDecision(Exception):
    def __init__(self, outcome: str):
        self.outcome = outcome
        super().__init__(outcome)


def _route_project_fields(
    conn, *, proposed_fields: dict[str, Any],
    post_commit: list[Callable[[], None]] | None,
    pct_commands: list[PctCompletionCommandReceipt] | None = None,
    source_pct_requested: bool = False,
    review_commands: list[ProjectFieldReviewCommandReceipt] | None = None,
    effect_commands: list[ProjectFieldEffectCommandReceipt] | None = None, **route_kwargs: Any,
) -> str:
    """Keep source-authorized PCT and speculative sibling fields distinct.

    A new PCT completion is a valid independent command even when a sibling
    field proposal is skipped. The caller still owns the final transaction and
    dispatches only callbacks belonging to effects that survived their boundary.
    """
    if proposed_fields.get("current_milestone") == "M8_GoLive" or source_pct_requested:
        import logging

        from src.db.borrowed_transaction import BorrowedTransactionConnection
        from src.db.savepoint import savepoint
        from src.email.pct_completion_routing import (
            try_complete_pct, without_completed_pct_fields,
        )

        pct_effects: list[Callable[[], None]] = []
        pct_command = None
        try:
            with savepoint(conn, ensure_transaction=True):
                pct_result = try_complete_pct(
                    BorrowedTransactionConnection(conn),
                    route_kwargs["project_id"], route_kwargs.get("source_ref"),
                    post_commit=pct_effects,
                )
                if pct_result and pct_commands is not None:
                    if pct_result["outcome"] not in {"applied", "duplicate"}:
                        raise ValueError("Unknown PCT command outcome")
                    pct_command = PctCompletionCommandReceipt(
                        project_id=pct_result["project_id"], receipt_id=pct_result["project_id"],
                        source_ref=pct_result["source_ref"], source_hash=pct_result["source_hash"],
                        evidence_id=pct_result["evidence_id"], update_id=pct_result["update_id"],
                        ticket_ids=pct_result["ticket_ids"],
                        status=("applied_in_application_transaction"
                                if pct_result["outcome"] == "applied" else "already_completed"),
                    )
                    if (pct_command.project_id != route_kwargs["project_id"]
                            or pct_command.source_ref != route_kwargs.get("source_ref")):
                        raise ValueError("PCT command must match inbound project and source")
        except Exception:
            logging.getLogger(__name__).exception(
                "Legacy inbound PCT command failed for %s", route_kwargs.get("project_id"),
            )
            return "field_route_failed"
        if pct_result:
            if pct_command is not None:
                pct_commands.append(pct_command)
            if post_commit is not None:
                post_commit.extend(pct_effects)
            siblings = without_completed_pct_fields(proposed_fields)
            pct_applied = pct_result["outcome"] == "applied"
            if not siblings:
                return "pct_completed" if pct_applied else "skipped"
            sibling_outcome = _route_regular_project_fields(
                conn, proposed_fields=siblings,
                post_commit=post_commit, review_commands=review_commands,
                effect_commands=effect_commands, **route_kwargs,
            )
            # A skipped/low-confidence sibling cannot erase the authorized
            # completion or turn it into a misleading no-op presentation.
            if pct_applied and sibling_outcome in {
                "skipped", "below_review_threshold", "assessment_required",
            }:
                return "pct_completed"
            return sibling_outcome
    return _route_regular_project_fields(
        conn, proposed_fields=proposed_fields,
        post_commit=post_commit, review_commands=review_commands, effect_commands=effect_commands, **route_kwargs,
    )


def _route_regular_project_fields(
    conn, *, proposed_fields: dict[str, Any],
    post_commit: list[Callable[[], None]] | None,
    review_commands: list[ProjectFieldReviewCommandReceipt] | None = None,
    effect_commands: list[ProjectFieldEffectCommandReceipt] | None = None, **route_kwargs: Any,
) -> str:
    """Isolate one speculative field decision from source evidence and caller work."""
    import logging

    from src.db.borrowed_transaction import BorrowedTransactionConnection
    from src.db.savepoint import savepoint
    from src.email.reply_analyser import route_proposed_field_updates
    from src.email.update_outcomes import COMMITTED_UPDATE_OUTCOMES

    staged: list[Callable[[], None]] = []
    staged_reviews = [] if review_commands is not None else None
    staged_effects = [] if effect_commands is not None else None
    try:
        with savepoint(conn, ensure_transaction=True):
            outcome = route_proposed_field_updates(
                BorrowedTransactionConnection(conn), proposed_fields=proposed_fields,
                post_commit=staged, raise_on_error=True, **route_kwargs,
                **({"review_commands": staged_reviews} if staged_reviews is not None else {}),
                **({"effect_commands": staged_effects} if staged_effects is not None else {}),
            )
            if outcome in {
                "skipped", "below_review_threshold", "assessment_required",
                "review_persistence_failed",
            }:
                raise _NonpersistentFieldDecision(outcome)
            if outcome not in COMMITTED_UPDATE_OUTCOMES | {
                "review_card", "review_notification_pending", "review_notification_unknown",
            }:
                raise ValueError(f"Unknown legacy email field outcome: {outcome!r}")
            if staged_reviews is not None:
                if outcome in FIELD_REVIEW_OUTCOMES:
                    if len(staged_reviews) != 1:
                        raise ValueError("Claimed field review requires one actual command identity")
                    staged_reviews[0] = validate_field_review_command(
                        conn, staged_reviews[0], outcome=outcome, **route_kwargs,
                    )
                elif staged_reviews:
                    raise ValueError("Field review identity cannot certify a non-review outcome")
            if staged_effects is not None:
                if outcome in FIELD_EFFECT_OUTCOMES:
                    if len(staged_effects) != 1:
                        raise ValueError("Claimed field effect requires one actual command identity")
                    staged_effects[0] = validate_field_effect_command(
                        conn, staged_effects[0], outcome=outcome, **route_kwargs,
                    )
                elif staged_effects:
                    raise ValueError("Field effect identity cannot certify a non-effect outcome")
    except _NonpersistentFieldDecision as rejected:
        return rejected.outcome
    except Exception:
        logging.getLogger(__name__).exception(
            "Legacy inbound field command failed for %s", route_kwargs.get("project_id"),
        )
        return "field_route_failed"
    if post_commit is not None:
        post_commit.extend(staged)
    if review_commands is not None:
        review_commands.extend(staged_reviews)
    if effect_commands is not None:
        effect_commands.extend(staged_effects)
    return outcome


def validate_field_review_command(conn, command, *, outcome, project_id, source_ref, evidence_ref, **_):
    """Correlate the writer-exported identity with its actual source, proposal and intent."""
    import hashlib
    import json

    from config.defaults import EMAIL_BODY_SNIPPET_CHARS

    command = ProjectFieldReviewCommandReceipt.model_validate(command.model_dump())
    if (not conn.in_transaction or command.project_id != project_id
            or command.source_ref != source_ref or command.evidence_id != evidence_ref
            or command.write_action != outcome):
        raise ValueError("Field review identity must match the caller's source-bound command")
    source = conn.execute(
        "SELECT project_id,sender_email,body_snippet FROM processed_emails WHERE email_id=?",
        (source_ref,),
    ).fetchone()
    evidence = conn.execute(
        "SELECT project_id,source_ref,content_text FROM evidence_store WHERE evidence_id=?",
        (command.evidence_id,),
    ).fetchone()
    if source is None or source[0] != project_id or evidence is None or tuple(evidence) != (
        project_id, source_ref, (source[2] or "")[:EMAIL_BODY_SNIPPET_CHARS],
    ):
        raise ValueError("Field review evidence must match its persisted email source")
    source_hash = hashlib.sha256(
        ((source[1] or "") + "\0" + (source[2] or "")).encode("utf-8"),
    ).hexdigest()
    if command.source_hash != source_hash:
        raise ValueError("Field review content hash must match its persisted email source")
    proposal = conn.execute(
        "SELECT project_id,source_ref,decision_outcome,proposed_changes FROM proposed_updates WHERE update_id=?",
        (command.update_id,),
    ).fetchone()
    delivery = conn.execute(
        "SELECT project_id,payload_json FROM email_update_notification_outbox WHERE update_id=?",
        (command.delivery_intent_id,),
    ).fetchone()
    if (proposal is None or tuple(proposal[:3]) != (project_id, source_ref, "needs_review")
            or delivery is None or delivery[0] != project_id):
        raise ValueError("Field review requires its real source-bound proposal and delivery intent")
    changes, payload = json.loads(proposal[3]), json.loads(delivery[1])
    if (not changes or len({c["field"] for c in changes}) != len(changes)
            or {c["field"]: c["new_value"] for c in changes} != command.proposed_fields
            or {c["evidence_ref"] for c in changes} != {command.evidence_id}
            or payload.get("project_id") != project_id or payload.get("update_id") != command.update_id
            or payload.get("_notification_kind") != "review_card"
            or payload.get("evidence_refs") != [command.evidence_id]
            or payload.get("proposed_changes") != changes):
        raise ValueError("Field review proposal content and intent must match the command identity")
    return command


def persist_inbound_project_analysis(
    conn, *, event: Any, fetched: Any, body: str, stored: dict[str, Any], logger: Any,
    post_commit: list[Callable[[], None]] | None = None,
    require_command_identity: bool = False,
) -> ProjectAnalysisWriteResult:
    """Apply a stored agent analysis using the caller's connection and review gate."""
    from config import defaults
    from config.defaults import EMAIL_BODY_SNIPPET_CHARS

    from src.email.milestone_inference import backfill_proposed_fields_from_analysis
    from src.email.project_state_assessment import apply_email_project_state_policy
    from src.repositories.evidence_repo import insert_evidence
    from src.email.analysis_guards import adjust_email_analysis_result
    from src.email.reply_analyser import route_proposed_field_updates
    from src.email.sender_trust import evidence_summary_for_sender
    from src.write_layer.write_helpers import get_project_field
    from datetime import datetime, timezone
    proposed_fields = stored.get("proposed_fields") or {}
    # Retain only the supported legacy request, never an inferred M8 permission.
    # The source-authorized service must verify persisted evidence before writing.
    source_pct_requested = (
        not defaults.PROJECT_STATE_EVIDENCE_GROUNDED_ENABLED
        and proposed_fields.get("current_milestone") == "M8_GoLive"
    )
    confidence = stored.get("confidence", 0.0)
    write_action = "skipped"
    pct_commands: list[PctCompletionCommandReceipt] = []
    review_commands = [] if require_command_identity else None
    effect_commands = [] if require_command_identity else None
    current_ms = get_project_field(conn, event.project_id, "current_milestone")
    current_na = get_project_field(conn, event.project_id, "next_action")
    pre_na = (proposed_fields or {}).get("next_action")
    proposed_fields = backfill_proposed_fields_from_analysis(
        proposed_fields,
        summary=stored.get("summary") or "",
        suggested_action=stored.get("suggested_action") or "",
        body=body,
        current_milestone=current_ms,
        current_next_action=current_na,
        confidence=float(stored.get("confidence") or 0.0),
        allow_milestone=False,
    )
    state_result = apply_email_project_state_policy(
        conn,
        project_id=event.project_id,
        proposed_fields=proposed_fields,
        assessment_detail=stored.get("assessment_detail"),
        source_ref=event.email_id,
        sender_email=event.sender_email,
        received_at=getattr(fetched, "received_at", None),
        body=body,
        attachments=getattr(fetched, "attachments", None),
    )
    proposed_fields = state_result.proposed_fields
    # Progress backfill filled next_action → ensure write gate can fire
    post_na = (proposed_fields or {}).get("next_action")
    if post_na and post_na != pre_na and confidence < 0.95:
        confidence = max(float(confidence or 0.0), 0.95)
    email_intent = stored.get("email_intent", "")
    has_attachments = bool(getattr(fetched, "attachments", None))
    confidence, proposed_fields = adjust_email_analysis_result(
        body,
        confidence,
        proposed_fields,
        sender_email=event.sender_email,
        email_intent=email_intent,
        has_attachments=has_attachments,
        assessment=state_result.assessment, project_id=event.project_id, source_ref=event.email_id,
    )
    shadow_baseline_fields = {
        str(name): str(value)
        for name, value in (proposed_fields or {}).items()
    }
    if proposed_fields or source_pct_requested:
        evidence_id = insert_evidence(
            conn,
            project_id=event.project_id,
            evidence_type="email",
            content_summary=evidence_summary_for_sender(
                event.sender_email,
                event.subject or "",
            ),
            evidence_at=datetime.now(timezone.utc).isoformat(),
            content_text=body[:EMAIL_BODY_SNIPPET_CHARS],
            source_ref=event.email_id,
        )
        write_action = _route_project_fields(
            conn,
            project_id=event.project_id,
            proposed_fields=proposed_fields,
            confidence=confidence,
            evidence_ref=evidence_id,
            actor="email_agent",
            reason=f"Inbound email from {event.sender_email}",
            summary=stored.get("summary") or "",
            suggested_action=stored.get("suggested_action") or "",
            sender_email=event.sender_email,
            email_body=body,
            email_intent=email_intent,
            has_attachments=has_attachments,
            subject=event.subject,
            assessment=(
                state_result.assessment.json_payload()
                if state_result.assessment is not None else None
            ),
            policy_version=(
                state_result.assessment.policy_version
                if state_result.assessment is not None else None
            ),
            source_type=(
                state_result.assessment.source_type.value
                if state_result.assessment is not None else None
            ),
            source_ref=event.email_id,
            post_commit=post_commit,
            pct_commands=pct_commands,
            source_pct_requested=source_pct_requested,
            review_commands=review_commands, effect_commands=effect_commands,
        )
        if write_action in {"review_persistence_failed", "field_route_failed"}:
            # The proposal/intent did not commit; discard this uncommitted
            # evidence rather than leaving a misleading orphan behind.
            conn.execute("DELETE FROM evidence_store WHERE evidence_id=?", (evidence_id,))
        if write_action == "pct_completed":
            from src.email.pct_completion_routing import completion_summary
            stored = {**stored, "summary": completion_summary(conn, event.project_id), "suggested_action": ""}
            proposed_fields = {"current_milestone":"M8_GoLive", "project_status":"Completed", "next_action":"", "ball_with":"Unknown"}
        logger.info(
            "Inbound proposed_fields for %s: %s (confidence=%.2f, action=%s)",
            event.project_id,
            list(proposed_fields.keys()),
            confidence,
            write_action,
        )
    return ProjectAnalysisWriteResult(
        write_action=write_action,
        baseline_fields=shadow_baseline_fields,
        stored=stored,
        pct_command=pct_commands[0] if pct_commands else None,
        field_review_command=review_commands[0] if review_commands else None,
        field_effect_command=effect_commands[0] if effect_commands else None,
    )


def persist_fallback_project_summary(
    conn, *, event: Any, body: str, email_summary: dict[str, Any], extract_fields,
    post_commit: list[Callable[[], None]] | None = None,
) -> dict[str, str]:
    """Persist the legacy three-field fallback in a caller-owned write scope.

    Review proposals commit their source evidence and delivery intent together.
    Other routing outcomes retain caller-owned evidence persistence.
    """
    from datetime import datetime, timezone

    from config.defaults import EMAIL_BODY_SNIPPET_CHARS
    from src.repositories.evidence_repo import insert_evidence
    from src.write_layer.write_helpers import get_project_field

    current_ms = get_project_field(conn, event.project_id, "current_milestone")
    fields = extract_fields(email_summary, current_milestone=current_ms)
    baseline = {str(name): str(value) for name, value in (fields or {}).items()}
    if fields:
        evidence_id = insert_evidence(
            conn,
            project_id=event.project_id,
            evidence_type="email",
            content_summary=(
                f"Fallback inbound email from {event.sender_email}: "
                f"{(event.subject or '')[:120]}"
            ),
            evidence_at=datetime.now(timezone.utc).isoformat(),
            content_text=body[:EMAIL_BODY_SNIPPET_CHARS],
            source_ref=event.email_id,
        )
        outcome = _route_project_fields(
            conn,
            project_id=event.project_id,
            proposed_fields=fields,
            confidence=max(float(email_summary.get("confidence") or 0.0), 0.85),
            evidence_ref=evidence_id,
            actor="email_inbound",
            reason=f"Inbound email fallback from {event.sender_email}",
            summary=(email_summary.get("summary") or "")[:200],
            suggested_action=(email_summary.get("action_requested") or "")[:200],
            sender_email=event.sender_email,
            email_body=body,
            post_commit=post_commit,
        )
        if outcome in {"review_persistence_failed", "field_route_failed"}:
            conn.execute("DELETE FROM evidence_store WHERE evidence_id=?", (evidence_id,))
    return baseline
