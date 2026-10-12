"""Synchronous serving path for evidence-grounded inbound email project updates.

The serving path performs one bounded fact-extraction call, validates exact
citations, evaluates project state deterministically, and exposes only a
sanitized proposal to the existing write/review layer. Execution failures are
returned to the caller so the legacy path can be used as a fallback. Valid
abstentions are successful outcomes and must not trigger the legacy path.
"""

from __future__ import annotations

import hashlib
import json
import logging
import time
from dataclasses import dataclass, field, replace
from typing import Any, Callable, Literal

from config.settings import CONFIG
from src.email.inbound_work import PctCompletionCommandReceipt
from src.email.action_bundle_receipt import ActionBundleCommandReceipt
from src.email.project_truth.citations import validate_extraction_citations
from src.email.project_truth.contracts import (
    EmailProjectRecommendation,
    IndependentStateCommandReceipt,
    FieldRouteCommandReceipt,
    EmailActionChange,
    EmailProjectTruthCase,
    PipelineCallMetadata,
    ValidatedEmailEvidence,
)
from src.email.project_truth.evaluator import evaluate_validated_evidence
from src.email.project_truth.extraction import run_fact_extraction
from src.email.project_truth.shadow import (
    EmailProjectTruthShadowJob,
    build_shadow_case,
)
from src.models.enums import ProjectStatus
from src.models.progress_assessment import AssessmentCode
from src.observability.contracts import TraceContext, trace_context_scope
from src.observability.lifecycle import emit_event, emit_run_finish, emit_run_start
from src.utils.operator_language import read_response_language
from src.email.project_truth.serve_renderer import (
    render_email_project_truth_operator_text,
)


logger = logging.getLogger(__name__)

EMAIL_PROJECT_TRUTH_SERVE_VERSION = "email-project-truth-serve-v1"
ALLOWED_PROJECT_FIELDS = frozenset(
    {"current_milestone", "project_status", "ball_with", "next_action"}
)

Extract = Callable[..., Any]


@dataclass(frozen=True)
class LagReviewCommandReceipt:
    """A source-bound preview outcome; a proposal is never a milestone write."""

    project_id: str
    source_ref: str
    preview_result: str
    status: Literal["persisted_in_caller_transaction", "committed", "not_applied", "incomplete"]
    proposed_update_id: str | None = None
    proposed_fields: tuple[tuple[str, str], ...] = ()
    source_type: Literal["email"] = "email"


@dataclass(frozen=True)
class _LagReviewStateReceipt(IndependentStateCommandReceipt):
    lag_review_command: LagReviewCommandReceipt | None = None


@dataclass(frozen=True)
class EmailProjectTruthServeResult:
    """One synchronous B-path decision, including only sanitized serving fields."""

    execution_status: Literal["completed", "failed"]
    project_id: str
    source_ref: str
    elapsed_ms: float
    extraction_ms: int = 0
    model: str | None = None
    safe_proposed_fields: tuple[tuple[str, str], ...] = ()
    safety_reasons: tuple[str, ...] = ()
    review_reasons: tuple[str, ...] = ()
    citation_coverage: float = 0.0
    accepted_citations: int = 0
    total_citations: int = 0
    rejected_citations: int = 0
    action_changes: tuple[EmailActionChange, ...] = ()
    case: EmailProjectTruthCase | None = field(default=None, repr=False)
    evidence: ValidatedEmailEvidence | None = field(default=None, repr=False)
    recommendation: EmailProjectRecommendation | None = field(default=None, repr=False)
    trace_id: str | None = None
    run_id: str | None = None
    error_class: str | None = None
    error_message: str | None = field(default=None, repr=False)

    @property
    def completed(self) -> bool:
        return self.execution_status == "completed"

    @property
    def proposed_fields(self) -> dict[str, str]:
        return dict(self.safe_proposed_fields)

    @property
    def abstained(self) -> bool:
        return self.completed and not self.safe_proposed_fields

    def safe_metadata(self) -> dict[str, Any]:
        return {
            "serve_version": EMAIL_PROJECT_TRUTH_SERVE_VERSION,
            "execution_status": self.execution_status,
            "project_id": self.project_id,
            "elapsed_ms": round(self.elapsed_ms, 3),
            "extraction_ms": self.extraction_ms,
            "model": self.model,
            "candidate_fields": [name for name, _value in self.safe_proposed_fields],
            "safety_reasons": list(self.safety_reasons),
            "review_reason_count": len(self.review_reasons),
            "citation_coverage": round(self.citation_coverage, 4),
            "accepted_citations": self.accepted_citations,
            "total_citations": self.total_citations,
            "rejected_citations": self.rejected_citations,
            "abstained": self.abstained,
            "error_class": self.error_class,
        }


@dataclass(frozen=True)
class EmailProjectTruthServeApplicationResult:
    """Outcome of routing a completed B decision through existing write controls."""

    status: Literal["completed", "failed"]
    write_action: str
    summary: str
    suggested_action: str
    proposed_fields: tuple[tuple[str, str], ...] = ()
    should_push_info_card: bool = False
    state_write_action: str | None = None
    state_command: IndependentStateCommandReceipt | None = None
    pct_command: PctCompletionCommandReceipt | None = None
    field_command: FieldRouteCommandReceipt | None = None
    lag_review_command: LagReviewCommandReceipt | None = None
    action_command: ActionBundleCommandReceipt | None = None
    action_review_id: str | None = None
    action_review_status: str | None = None
    state_proposed_fields: tuple[tuple[str, str], ...] = ()
    confidence: float = 0.0
    error_class: str | None = None

    @property
    def requires_reconciliation(self) -> bool:
        """A completed sibling command does not clear an unresolved failure."""
        return (
            self.status != "completed"
            or (self.state_command is not None and self.state_command.status == "incomplete")
            or (self.field_command is not None and self.field_command.status == "incomplete")
            or (self.lag_review_command is not None and self.lag_review_command.status == "incomplete")
            or self.write_action in {
                "action_review_persistence_failed",
                "review_persistence_failed",
            }
            or self.state_write_action in {
                "state_processing_failed",
                "application_failed",
                "review_persistence_failed",
            }
        )


@dataclass(frozen=True)
class _SanitizedProposal:
    fields: tuple[tuple[str, str], ...]
    safety_reasons: tuple[str, ...]


def email_project_truth_serve_enabled() -> bool:
    """Return whether synchronous Email Project Truth serving is enabled."""
    from config import defaults

    return getattr(defaults, "EMAIL_PROJECT_TRUTH_MODE", "off") == "serve"


def run_email_project_truth_serve(
    job: EmailProjectTruthShadowJob,
    *,
    model_override: str | None = None,
    timeout_seconds: int = 20,
    extractor: Extract | None = None,
) -> EmailProjectTruthServeResult:
    """Run one B-path serve decision without performing project writes.

    Exceptions are converted into ``failed`` results. Callers may fall back to
    the legacy A path only for that execution-failure result.
    """
    started = time.monotonic()
    source_ref_hash = hashlib.sha256(job.source_ref.encode("utf-8")).hexdigest()
    trace_context = TraceContext.create(
        surface="email",
        task="email_project_truth_serve",
        thread_id=job.thread_id,
        turn_id=job.turn_id,
        trace_id=job.parent_trace_id,
    )

    with trace_context_scope(trace_context):
        _safe_run_start(
            trace_context,
            metadata={
                "serve_version": EMAIL_PROJECT_TRUTH_SERVE_VERSION,
                "project_id": job.project_id,
                "source_ref_hash": source_ref_hash,
                "production_path": "B",
            },
            payload={
                "source_ref": job.source_ref,
                "subject": job.subject,
                "sender_email": job.sender_email,
                "body": job.body,
                "project_snapshot": job.project_snapshot.model_dump(mode="json"),
            },
        )
        try:
            case = build_shadow_case(job).model_copy(
                update={
                    "case_id": f"SERVE-{source_ref_hash[:16]}",
                    "category": "production_serve",
                }
            )
            extraction_fn = extractor or run_fact_extraction
            extraction, call_metadata = extraction_fn(
                case,
                model_override=model_override or None,
                timeout_sec=max(1, int(timeout_seconds)),
            )
            if not isinstance(call_metadata, PipelineCallMetadata):
                call_metadata = PipelineCallMetadata.model_validate(call_metadata)
            _safe_event(
                trace_context,
                "email_project_truth.extraction",
                status="completed",
                duration_ms=float(call_metadata.latency_ms),
                metadata={
                    "model": call_metadata.model,
                    "source_ref_hash": source_ref_hash,
                    "fact_count": len(extraction.facts),
                    "uncertainty_count": len(extraction.uncertainties),
                    "contradiction_note_count": len(extraction.contradiction_notes),
                },
                payload={"extraction": extraction.model_dump(mode="json")},
            )
            evidence = validate_extraction_citations(case, extraction)
            recommendation = evaluate_validated_evidence(case, evidence)
            sanitized = sanitize_serve_proposals(
                job=job,
                evidence=evidence,
                recommendation=recommendation,
            )
            elapsed_ms = (time.monotonic() - started) * 1000
            result = EmailProjectTruthServeResult(
                execution_status="completed",
                project_id=job.project_id,
                source_ref=job.source_ref,
                elapsed_ms=elapsed_ms,
                extraction_ms=call_metadata.latency_ms,
                model=call_metadata.model or None,
                safe_proposed_fields=sanitized.fields,
                safety_reasons=sanitized.safety_reasons,
                review_reasons=tuple(recommendation.review_reasons),
                citation_coverage=recommendation.state_change_citation_coverage,
                accepted_citations=evidence.accepted_citations,
                total_citations=evidence.total_citations,
                rejected_citations=len(evidence.rejected_citations),
                action_changes=tuple(extraction.action_changes),
                case=case,
                evidence=evidence,
                recommendation=recommendation,
                trace_id=trace_context.trace_id,
                run_id=trace_context.run_id,
            )
            _safe_event(
                trace_context,
                "email_project_truth.serve_decision",
                status="completed",
                duration_ms=elapsed_ms,
                metadata=result.safe_metadata(),
                payload={
                    "case": case.model_dump(mode="json"),
                    "validated_evidence": evidence.model_dump(mode="json"),
                    "candidate_recommendation": recommendation.model_dump(mode="json"),
                    "sanitized_proposed_fields": result.proposed_fields,
                },
            )
            _safe_run_finish(
                trace_context,
                status="completed",
                exit_reason="abstained" if result.abstained else "proposal_ready",
                persistence_status="not_started",
                metadata=result.safe_metadata(),
            )
            return result
        except Exception as exc:
            elapsed_ms = (time.monotonic() - started) * 1000
            result = EmailProjectTruthServeResult(
                execution_status="failed",
                project_id=job.project_id,
                source_ref=job.source_ref,
                elapsed_ms=elapsed_ms,
                trace_id=trace_context.trace_id,
                run_id=trace_context.run_id,
                error_class=type(exc).__name__,
                error_message=str(exc),
            )
            _safe_event(
                trace_context,
                "email_project_truth.serve_decision",
                status="error",
                duration_ms=elapsed_ms,
                metadata=result.safe_metadata(),
                payload={"error": str(exc), "source_ref": job.source_ref},
            )
            _safe_run_finish(
                trace_context,
                status="error",
                exit_reason=type(exc).__name__,
                persistence_status="not_started",
                anomaly_flags=["serve_execution_failure"],
                metadata=result.safe_metadata(),
            )
            return result


def sanitize_serve_proposals(
    *,
    job: EmailProjectTruthShadowJob,
    evidence: ValidatedEmailEvidence,
    recommendation: EmailProjectRecommendation,
) -> _SanitizedProposal:
    """Fail closed unless every serving invariant is satisfied."""
    raw = recommendation.proposed_fields
    reasons: list[str] = []

    if set(raw) - ALLOWED_PROJECT_FIELDS:
        reasons.append("unknown_project_field")
    if not evidence.project_identity_resolved:
        reasons.append("project_identity_unresolved")
    elif evidence.resolved_project_id != job.project_id:
        reasons.append("project_identity_mismatch")
    if recommendation.assessment.project_id != job.project_id:
        reasons.append("assessment_project_mismatch")
    if AssessmentCode.INVALID_STATE_TRANSITION in recommendation.assessment.assessment_codes:
        reasons.append("later_stage_milestone_lag")
    elif recommendation.assessment.contradictions:
        reasons.append("contradictory_evidence")
    if job.project_snapshot.project_status in ProjectStatus.terminal_statuses() and raw:
        reasons.append("terminal_project_protection")
    if raw and recommendation.state_change_citation_coverage < 1.0:
        reasons.append("incomplete_state_change_citations")

    if reasons:
        return _SanitizedProposal(fields=(), safety_reasons=tuple(dict.fromkeys(reasons)))

    normalized = tuple(
        sorted(
            (str(name), str(value))
            for name, value in raw.items()
            if name in ALLOWED_PROJECT_FIELDS and value is not None
        )
    )
    return _SanitizedProposal(fields=normalized, safety_reasons=())


class SourceAuthorityChanged(ValueError):
    """The source binding captured before extraction is no longer applicable."""


def _current_source_authorized(conn, result, sender_email):
    from src.email.source_authority import normalized_sender, resolve_source_authority
    from src.models.progress_assessment import EvidenceLevel, EvidenceSourceType
    case = result.case
    if case is None or case.project_id != result.project_id or case.source_ref != result.source_ref:
        return False
    if normalized_sender(sender_email) != normalized_sender(case.sender_email):
        return False
    current = resolve_source_authority(conn, project_id=result.project_id, sender_email=sender_email)
    if current.evidence_level == EvidenceLevel.L5_UNVERIFIED_OR_INDIRECT:
        return False
    sources = [source for source in case.sources
        if source.source_ref == result.source_ref and source.source_type == EvidenceSourceType.EMAIL]
    return len(sources) == 1 and sources[0].actor == current.actor and sources[0].evidence_level == current.evidence_level


def _route_independent_state(conn, result, *, sender_email, subject, confidence, post_commit=None):
    """Preserve independent milestone/status decisions without overwriting a plan.

Never describe a state write as committed without checking persisted values.
An action transaction already committed must not be reported failed by this path.
"""
    def receipt(action, fields, status="not_applied", *, evidence_id=None, update_id=None, review_id=None):
        return IndependentStateCommandReceipt(
            project_id=result.project_id, source_ref=result.source_ref,
            write_action=action, status=status,
            proposed_fields=tuple(sorted(fields.items())),
            evidence_id=evidence_id, update_id=update_id, review_id=review_id,
        )

    if result.safety_reasons == ("later_stage_milestone_lag",):
        try:
            if not _current_source_authorized(conn, result, sender_email):
                return receipt("state_review_required", {})
            from src.email.project_truth.lag_review import route_lag_review
            review = route_lag_review(conn, result, language=read_response_language(conn), post_commit=post_commit)
            return _LagReviewStateReceipt(
                project_id=result.project_id, source_ref=result.source_ref,
                write_action=review.write_action, proposed_fields=review.proposed_fields,
                status="incomplete" if review.status == "failed" else "not_applied",
                lag_review_command=review.lag_review_command,
            )
        except Exception:
            logger.exception("Historical lag review did not finish for %s", result.project_id)
            return receipt("state_processing_failed", {}, "incomplete")
    fields={key:value for key,value in result.proposed_fields.items()
            if key in {"current_milestone", "project_status"}}
    if not fields:
        return receipt("not_applicable", {})
    try:
        if not _current_source_authorized(conn, result, sender_email):
            return receipt("state_review_required", fields)
        from src.email.body_cleaner import clean_email_body
        from src.email.source_authority import normalized_sender
        source=conn.execute("SELECT project_id,body_snippet,sender_email FROM processed_emails WHERE email_id=?",(result.source_ref,)).fetchone()
        if (not source or source["project_id"] != result.project_id
                or normalized_sender(source["sender_email"]) != normalized_sender(sender_email)
                or clean_email_body(source["body_snippet"] or "") != result.case.newest_authored_body):
            return receipt("state_review_required", fields)
        row=conn.execute("SELECT current_milestone,project_status FROM projects WHERE project_id=?",(result.project_id,)).fetchone()
        if not row:
            return receipt("state_review_required", fields)
        if all(row[key]==value for key,value in fields.items()):
            return receipt("no_change", fields)
        snapshot=result.case.project_snapshot
        expected_ms=getattr(snapshot.current_milestone,"value",snapshot.current_milestone)
        expected_status=getattr(snapshot.project_status,"value",snapshot.project_status)
        if row["current_milestone"]!=expected_ms or row["project_status"]!=expected_status:
            return receipt("state_review_required", fields)
        from datetime import datetime, timezone
        from src.db.borrowed_transaction import BorrowedTransactionConnection
        from src.repositories.evidence_repo import insert_evidence
        from src.email.reply_analyser import route_proposed_field_updates
        from src.email.update_outcomes import COMMITTED_UPDATE_OUTCOMES
        # The sibling action may already be committed. Enclose only this state
        # decision (including its evidence and review/outbox intent) in a new
        # command boundary. An outer savepoint alone would commit on RELEASE
        # when SQLite is in autocommit mode, so begin an owned transaction first.
        from uuid import uuid4

        owns_state_transaction = not conn.in_transaction
        if owns_state_transaction:
            conn.execute("BEGIN")
        savepoint_name = "independent_state_" + uuid4().hex
        conn.execute("SAVEPOINT " + savepoint_name)
        savepoint_open = True
        state_effects: list[Callable[[], None]] = []
        borrowed = BorrowedTransactionConnection(conn)
        evidence = insert_evidence(
            borrowed, result.project_id, "email", "Independent email state evidence",
            datetime.now(timezone.utc).isoformat(),
            content_text=result.case.newest_authored_body, source_ref=result.source_ref,
        )
        # Capture the command's own new audit rows, never a prior source replay.
        update_watermark = conn.execute(
            "SELECT COALESCE(MAX(rowid), 0) FROM proposed_updates"
        ).fetchone()[0]
        assessment = result.recommendation.assessment
        outcome = route_proposed_field_updates(
            borrowed, project_id=result.project_id, proposed_fields=fields,
            confidence=confidence, evidence_ref=evidence,
            actor="email_project_truth_b", reason="Independent source-bound state change",
            sender_email=sender_email, subject=subject,
            email_body=result.case.newest_authored_body,
            assessment=assessment.json_payload(), policy_version=assessment.policy_version,
            source_type="email", source_ref=result.source_ref, post_commit=state_effects,
            raise_on_error=True,
        )
        # Rejected and no-op routes own no evidence. Roll back *all* their
        # speculative writes, not just the evidence row; prior caller work and
        # any already-committed sibling action remain untouched.
        persisted_outcomes = COMMITTED_UPDATE_OUTCOMES | {
            "review_notification_pending", "review_notification_unknown",
        }
        if outcome not in persisted_outcomes:
            conn.execute("ROLLBACK TO SAVEPOINT " + savepoint_name)
            conn.execute("RELEASE SAVEPOINT " + savepoint_name)
            savepoint_open = False
            if owns_state_transaction:
                conn.rollback()
            return receipt(outcome, fields, "incomplete" if outcome == "review_persistence_failed" else "not_applied")
        if outcome in COMMITTED_UPDATE_OUTCOMES:
            actual = conn.execute(
                "SELECT current_milestone,project_status FROM projects WHERE project_id=?",
                (result.project_id,),
            ).fetchone()
            if not actual or not all(actual[key] == value for key, value in fields.items()):
                raise RuntimeError("Independent state write did not match persisted project fields")
        # The downstream adapter returns only a string. Resolve identifiers
        # from its newly inserted audit rows and this exact evidence UUID.
        rows = conn.execute(
            "SELECT update_id, proposed_changes, decision_outcome, task_type, source_type, source_ref "
            "FROM proposed_updates "
            "WHERE rowid > ? AND project_id=?",
            (update_watermark, result.project_id),
        ).fetchall()
        matches = []
        for candidate in rows:
            try:
                changes = json.loads(candidate["proposed_changes"])
            except (TypeError, ValueError):
                continue
            if changes and all(change.get("evidence_ref") == evidence for change in changes):
                matches.append(candidate)
        expected_decision = "needs_review" if outcome.startswith("review_") else "accepted"
        matching = [candidate for candidate in matches
                    if candidate["decision_outcome"] == expected_decision and (
                        (candidate["source_type"] == "email"
                         and candidate["source_ref"] == result.source_ref)
                        if expected_decision == "needs_review" else
                        (candidate["task_type"] == "direct_write" if outcome == "direct_write"
                         else candidate["task_type"] == "truth_write_reasoning")
                    )]
        update_id = matching[0]["update_id"] if len(matching) == 1 else None
        # Missing/ambiguous audit is incomplete, not an invented effect ID.
        command_status = "incomplete" if update_id is None else (
            "committed" if owns_state_transaction else "persisted_in_caller_transaction"
        )
        conn.execute("RELEASE SAVEPOINT " + savepoint_name)
        savepoint_open = False
        if owns_state_transaction:
            conn.commit()
        elif post_commit is not None:
            post_commit.extend(state_effects)
        # Owned writes have passed their durability boundary; borrowed writes
        # leave delivery to their caller after it exits the serializer scope.
        if owns_state_transaction:
            for effect in state_effects:
                try:
                    effect()
                except Exception:
                    logger.exception("Post-commit independent state effect failed for %s", result.project_id)
        return receipt(
            outcome, fields, command_status, evidence_id=evidence,
            update_id=update_id,
            review_id=update_id if expected_decision == "needs_review" else None,
        )
    except Exception:
        # A downstream premature commit destroys our savepoint. In that case
        # the outcome needs reconciliation: never delete evidence referenced by
        # a potentially committed review/write, nor roll back a new transaction
        # which we do not own. Do not infer commit from total_changes.
        if "savepoint_open" in locals() and savepoint_open:
            try:
                conn.execute("ROLLBACK TO SAVEPOINT " + savepoint_name)
                conn.execute("RELEASE SAVEPOINT " + savepoint_name)
                if owns_state_transaction:
                    conn.rollback()
            except Exception:
                logger.exception("Independent state boundary was lost for %s", result.project_id)
        elif "owns_state_transaction" in locals() and owns_state_transaction and conn.in_transaction:
            try:
                conn.rollback()
            except Exception:
                logger.exception("Independent state rollback failed for %s", result.project_id)
        logger.exception("Independent state routing did not finish for %s", result.project_id)
        return receipt("state_processing_failed", fields, "incomplete")



def apply_email_project_truth_serve_result(
    conn: Any,
    result: EmailProjectTruthServeResult,
    *,
    sender_email: str,
    subject: str,
    body: str,
    has_attachments: bool = False,
    post_commit: list[Callable[[], None]] | None = None,
) -> EmailProjectTruthServeApplicationResult:
    """Route a completed B decision through existing evidence/review/write logic.

    This function never invokes A. Any rendering, persistence, or card-routing
    failure is returned to the caller so it can alert the operator without
    duplicating model calls or side effects.
    """
    if not result.completed or result.recommendation is None:
        return EmailProjectTruthServeApplicationResult(
            status="failed",
            write_action="not_applicable",
            summary="",
            suggested_action="",
            error_class=result.error_class or "ServeResultIncomplete",
        )

    summary = ""
    suggested_action = ""
    proposed_fields = result.proposed_fields
    confidence = 0.0
    pct_command = None
    try:
        language = read_response_language(conn)
        summary, suggested_action = render_email_project_truth_operator_text(
            result,
            language=language,
        )
        confidence = _serve_confidence(result)
        from src.email.pct_completion_routing import try_complete_pct, completion_summary
        pct_result = try_complete_pct(conn, result.project_id, result.source_ref, post_commit=post_commit)
        pct_applied = bool(pct_result and pct_result["outcome"] == "applied")
        pct_command = PctCompletionCommandReceipt(
            project_id=result.project_id, receipt_id=pct_result["project_id"],
            source_ref=result.source_ref, source_hash=pct_result["source_hash"], evidence_id=pct_result["evidence_id"],
            update_id=pct_result["update_id"], ticket_ids=pct_result["ticket_ids"],
            status=("applied_in_application_transaction" if pct_applied else "already_completed"),
        ) if pct_result and "source_hash" in pct_result else None
        if pct_result:
            from src.email.pct_completion_routing import without_completed_pct_fields
            proposed_fields = without_completed_pct_fields(proposed_fields)
            if not proposed_fields and not result.action_changes:
                return EmailProjectTruthServeApplicationResult(
                    status="completed", write_action=(
                        "pct_completed" if pct_applied else "pct_already_completed"
                    ),
                    summary=completion_summary(conn, result.project_id), suggested_action="",
                    proposed_fields=tuple(sorted({"current_milestone":"M8_GoLive", "project_status":"Completed",
                        "next_action":"", "ball_with":"Unknown"}.items())),
                    should_push_info_card=pct_applied, confidence=1.0,
                    pct_command=pct_command,
                )
            # Both a first completion and a repeated receipt must preserve
            # sibling commands. A first completion joins the caller's pending
            # route transaction; sibling commands retain their safety gates.
            result = replace(result, safe_proposed_fields=tuple(sorted(proposed_fields.items())))
            if pct_applied:
                summary = completion_summary(conn, result.project_id) + "\n" + summary
        if result.safety_reasons == ("later_stage_milestone_lag",) and not result.action_changes:
            if not _current_source_authorized(conn, result, sender_email):
                raise SourceAuthorityChanged("Source authority changed; reassess before creating a milestone review.")
            from src.email.project_truth.lag_review import route_lag_review
            return replace(route_lag_review(conn, result, language=language, post_commit=post_commit),
                           pct_command=pct_command)
        if result.action_changes:
            from src.email.action_bundle_bridge import apply_email_action_changes
            action_caller_transaction = conn.in_transaction
            if result.case is None or result.evidence is None or not result.evidence.project_identity_resolved:
                action_result = None
            else:
                action_result = apply_email_action_changes(conn, case=result.case,
                    changes=result.action_changes, sender_email=sender_email)
            if action_result is not None and action_result.status in {"applied", "duplicate"}:
                # A duplicate is not a new command commit. Close an action-owned
                # transaction, but never commit unrelated caller-owned edits.
                if not action_caller_transaction:
                    try:
                        conn.commit()
                    except Exception:
                        conn.rollback()
                        raise
                current = action_result.result["current_plan"]
                if action_result.status == "applied":
                    def _publish_action_event() -> None:
                        try:
                            from src.events.bus import Event, event_bus
                            event_bus.publish(Event(event_type="project_updated", project_id=result.project_id,
                                surface="email", detail={"action_plan_revision": current["revision"]}))
                        except Exception:
                            logger.exception("Post-commit email action event failed")

                    if action_caller_transaction:
                        if post_commit is not None:
                            post_commit.append(_publish_action_event)
                    else:
                        _publish_action_event()
                state_command = _route_independent_state(conn, result,
                    sender_email=sender_email, subject=subject, confidence=confidence, post_commit=post_commit)
                state_outcome, state_fields = state_command
                if language == "en":
                    action_summary = f"Email action plan {action_result.status}; current revision {current['revision']}."
                else:
                    marker = "\u5df2\u5957\u7528" if action_result.status == "applied" else "\u6b64\u90f5\u4ef6\u5df2\u8655\u7406"
                    if language == "zh-CN":
                        marker = "\u5df2\u5e94\u7528" if action_result.status == "applied" else "\u6b64\u90ae\u4ef6\u5df2\u5904\u7406"
                    action_summary = f"{marker}\uff1baction plan revision {current['revision']}\u3002"
                return EmailProjectTruthServeApplicationResult(
                    pct_command=pct_command,
                    status="completed", write_action="email_action_bundle_" + action_result.status,
                    action_command=action_result.command,
                    summary=(completion_summary(conn, result.project_id) + "\n" if pct_applied else "")
                    + action_summary + (f" State decision: {state_outcome}." if state_fields else ""),
                    suggested_action=current["next_action"] or "",
                    state_write_action=state_outcome,
                    state_command=state_command if isinstance(state_command, IndependentStateCommandReceipt) else None,
                lag_review_command=(state_command.lag_review_command
                                    if isinstance(state_command, _LagReviewStateReceipt) else None),
                    state_proposed_fields=tuple(sorted(state_fields.items())),
                    proposed_fields=tuple(sorted({"ball_with": current["ball_with"], "next_action": current["next_action"] or ""}.items())),
                    # Replaying the action receipt must not generate another
                    # action/PCT information card; state uses its own routing.
                    should_push_info_card=action_result.status == "applied", confidence=confidence,
                )
            state_command = _route_independent_state(conn, result,
                sender_email=sender_email, subject=subject, confidence=confidence, post_commit=post_commit)
            state_outcome, state_fields = state_command
            review = None
            review_failure = ""
            try:
                from src.db.savepoint import savepoint
                from src.services.followup_review import queue_email_action_review
                # A review must not commit or roll back an existing caller scope.
                # Start a real transaction before the savepoint on idle SQLite
                # connections so RELEASE cannot make the review durable early.
                if not conn.in_transaction:
                    conn.execute("BEGIN")
                with savepoint(conn):
                    review = queue_email_action_review(conn, project_id=result.project_id, source_ref=result.source_ref,
                        case=result.case, changes=result.action_changes,
                        reason=action_result.reason if action_result is not None else "Source identity unavailable.")
                if not action_caller_transaction:
                    conn.commit()
            except Exception:
                if not action_caller_transaction and conn.in_transaction:
                    conn.rollback()
                review = None
                logger.exception("Action review could not be persisted for %s", result.project_id)
                review_failure = " Durable action review could not be saved; operator attention is required."
            if review and review["status"] == "pending":
                def _publish_action_review_event() -> None:
                    try:
                        from src.events.bus import Event, event_bus
                        event_bus.publish(Event(event_type="project_updated", project_id=result.project_id, surface="email",
                            detail={"action_review_id": review["review_id"], "action_review_status": "pending"}))
                    except Exception:
                        logger.exception("Post-commit action review event failed")

                if action_caller_transaction:
                    if post_commit is not None:
                        post_commit.append(_publish_action_review_event)
                else:
                    _publish_action_review_event()
            return EmailProjectTruthServeApplicationResult(
                pct_command=pct_command,
                status="completed", write_action="action_review_required" if review else "action_review_persistence_failed",
                action_review_id=review["review_id"] if review else None,
                action_review_status=review["status"] if review else None,
                summary=summary, suggested_action=(
                    f"Action changes were not applied. Independent state decision: {state_outcome}. "
                    if state_fields else "Review the proposed action-plan changes; no project fields were applied. "
                ) + (action_result.reason if action_result is not None else "Source identity unavailable.")
                  + (f" Action review: {review['review_id']} ({review['status']})." if review else review_failure),
                state_write_action=state_outcome,
                state_command=state_command if isinstance(state_command, IndependentStateCommandReceipt) else None,
                    lag_review_command=(state_command.lag_review_command
                                        if isinstance(state_command, _LagReviewStateReceipt) else None),
                state_proposed_fields=tuple(sorted(state_fields.items())),
                should_push_info_card=True, confidence=confidence,
            )
        if not proposed_fields:
            return EmailProjectTruthServeApplicationResult(
                pct_command=pct_command,
                status="completed",
                write_action="skipped",
                summary=summary,
                suggested_action=suggested_action,
                should_push_info_card=True,
                confidence=confidence,
            )

        if not _current_source_authorized(conn, result, sender_email):
            raise SourceAuthorityChanged("Source authority is missing or changed; reassess the email before applying updates.")

        from src.email.project_truth.field_route import route_serve_fields

        field_command = route_serve_fields(
            conn, result, proposed_fields=proposed_fields, confidence=confidence,
            sender_email=sender_email, subject=subject, body=body,
            summary=summary, suggested_action=suggested_action,
            has_attachments=has_attachments, post_commit=post_commit,
        )
        write_action = field_command.write_action
        return EmailProjectTruthServeApplicationResult(
            pct_command=pct_command,
            status="completed",
            write_action=write_action,
            summary=summary,
            suggested_action=suggested_action,
            proposed_fields=tuple(sorted(proposed_fields.items())),
            field_command=field_command,
            should_push_info_card=write_action not in {
                "review_card",
                "review_notification_pending",
                "review_notification_unknown",
                "auto_applied_undo_card",
                "auto_applied_notification_pending",
                "auto_applied_notification_unknown",
                "direct_write",
            },
            confidence=confidence,
        )
    except Exception as exc:
        return EmailProjectTruthServeApplicationResult(
            pct_command=pct_command,
            status="failed",
            write_action="application_failed",
            summary=summary,
            suggested_action=suggested_action,
            proposed_fields=tuple(sorted(proposed_fields.items())),
            should_push_info_card=bool(summary),
            confidence=confidence,
            error_class=type(exc).__name__,
        )

def _serve_confidence(result: EmailProjectTruthServeResult) -> float:
    if not result.safe_proposed_fields:
        return 0.0
    if not result.review_reasons:
        return 0.99
    review_threshold = float(CONFIG.write_layer.confidence_auto_threshold)
    auto_threshold = float(CONFIG.write_layer.auto_write_confidence_threshold)
    if not (0.0 <= review_threshold < auto_threshold <= 1.0):
        return 0.0
    candidate = max(review_threshold, min(0.94, auto_threshold - 1e-6))
    return candidate if candidate < auto_threshold else 0.0


def _safe_run_start(context: TraceContext, **kwargs: Any) -> None:
    try:
        emit_run_start(context, **kwargs)
    except Exception:
        pass


def _safe_event(context: TraceContext, event_type: str, **kwargs: Any) -> None:
    try:
        emit_event(context, event_type, **kwargs)
    except Exception:
        pass


def _safe_run_finish(context: TraceContext, **kwargs: Any) -> None:
    try:
        emit_run_finish(context, **kwargs)
    except Exception:
        pass


__all__ = [
    "ALLOWED_PROJECT_FIELDS",
    "EMAIL_PROJECT_TRUTH_SERVE_VERSION",
    "EmailProjectTruthServeApplicationResult",
    "EmailProjectTruthServeResult",
    "apply_email_project_truth_serve_result",
    "email_project_truth_serve_enabled",
    "render_email_project_truth_operator_text",
    "run_email_project_truth_serve",
    "sanitize_serve_proposals",
]
