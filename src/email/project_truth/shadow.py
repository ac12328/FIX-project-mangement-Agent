"""Side-effect-free Email Project Truth B shadow execution."""

from __future__ import annotations

import hashlib
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable, Mapping

from src.email.body_cleaner import clean_email_body
from src.email.source_authority import SourceAuthority
from src.email.project_truth.citations import validate_extraction_citations
from src.email.project_truth.contracts import (
    EmailProjectRecommendation,
    EmailProjectTruthCase,
    EvidenceDocument,
    ProjectSnapshot,
)
from src.email.project_truth.evaluator import evaluate_validated_evidence
from src.email.project_truth.extraction import run_fact_extraction
from src.email.project_truth.shadow_repository import (
    EmailProjectTruthShadowRecord,
    record_email_project_truth_shadow_fail_open,
)
from src.models.progress_assessment import EvidenceLevel, EvidenceSourceType
from src.observability.contracts import TraceContext, trace_context_scope
from src.observability.lifecycle import emit_event, emit_run_finish, emit_run_start


EMAIL_PROJECT_TRUTH_SHADOW_VERSION = "email-project-truth-shadow-v1"
_ALLOWED_PROJECT_FIELDS = frozenset(
    {"current_milestone", "project_status", "ball_with", "next_action"}
)
_MAX_BODY_CHARS = 12_000
_MAX_ATTACHMENT_CHARS_PER_FILE = 6_000
_MAX_ATTACHMENT_CHARS_TOTAL = 12_000


@dataclass(frozen=True)
class EmailProjectTruthShadowAttachment:
    filename: str
    content_type: str
    data: bytes
    size_bytes: int


@dataclass(frozen=True)
class EmailProjectTruthShadowJob:
    source_ref: str
    project_id: str
    subject: str
    sender_email: str
    received_at: str | None
    body: str
    project_snapshot: ProjectSnapshot
    attachments: tuple[EmailProjectTruthShadowAttachment, ...] = ()
    baseline_status: str = "completed"
    baseline_fields: tuple[tuple[str, str], ...] = ()
    baseline_summary: str = ""
    baseline_suggested_action: str = ""
    parent_trace_id: str | None = None
    parent_run_id: str | None = None
    thread_id: str | None = None
    turn_id: str | None = None
    enqueued_monotonic: float = 0.0
    source_authority: SourceAuthority | None = None
    sent_at: str | None = None


@dataclass(frozen=True)
class EmailProjectTruthShadowObservation:
    execution_status: str
    project_id: str
    comparison_classification: str
    elapsed_ms: float
    extraction_ms: int = 0
    queue_lag_ms: float = 0.0
    model: str | None = None
    baseline_fields: tuple[str, ...] = ()
    candidate_fields: tuple[str, ...] = ()
    citation_coverage: float = 0.0
    unsafe_update: bool = False
    review_required: bool = False
    accepted_citations: int = 0
    total_citations: int = 0
    rejected_citations: int = 0
    persisted: bool = False
    error_class: str | None = None
    shadow_trace_id: str | None = None
    shadow_run_id: str | None = None

    def safe_metadata(self) -> dict[str, Any]:
        return {
            "shadow_version": EMAIL_PROJECT_TRUTH_SHADOW_VERSION,
            "execution_status": self.execution_status,
            "project_id": self.project_id,
            "comparison_classification": self.comparison_classification,
            "elapsed_ms": round(self.elapsed_ms, 3),
            "extraction_ms": self.extraction_ms,
            "queue_lag_ms": round(self.queue_lag_ms, 3),
            "model": self.model,
            "baseline_fields": list(self.baseline_fields),
            "candidate_fields": list(self.candidate_fields),
            "citation_coverage": round(self.citation_coverage, 4),
            "unsafe_update": self.unsafe_update,
            "review_required": self.review_required,
            "accepted_citations": self.accepted_citations,
            "total_citations": self.total_citations,
            "rejected_citations": self.rejected_citations,
            "persisted": self.persisted,
            "error_class": self.error_class,
            "production_reply_changed": False,
            "production_project_write": False,
            "external_send": False,
        }


Persist = Callable[[EmailProjectTruthShadowRecord], str | None]
Extract = Callable[..., Any]


def observe_email_project_truth_shadow(
    job: EmailProjectTruthShadowJob,
    *,
    model_override: str | None = None,
    timeout_seconds: int = 20,
    queue_lag_ms: float = 0.0,
    extractor: Extract | None = None,
    persist: Persist | None = None,
) -> EmailProjectTruthShadowObservation:
    """Run one B-path candidate without changing A, project state, or transports."""
    started = time.monotonic()
    baseline_fields = dict(job.baseline_fields)
    source_ref_hash = _hash_text(job.source_ref)
    trace_context = TraceContext.create(
        surface="background",
        task="email_project_truth_shadow",
        thread_id=job.thread_id,
        turn_id=job.turn_id,
        trace_id=job.parent_trace_id,
    )
    persist_fn = persist or record_email_project_truth_shadow_fail_open

    with trace_context_scope(trace_context):
        _safe_run_start(
            trace_context,
            metadata={
                "shadow_version": EMAIL_PROJECT_TRUTH_SHADOW_VERSION,
                "parent_run_id": job.parent_run_id,
                "project_id": job.project_id,
                "source_ref_hash": source_ref_hash,
                "baseline_status": job.baseline_status,
                "production_reply_changed": False,
                "production_project_write": False,
                "external_send": False,
            },
            payload={
                "source_ref": job.source_ref,
                "subject": job.subject,
                "sender_email": job.sender_email,
                "body": job.body,
                "baseline_fields": baseline_fields,
                "baseline_summary": job.baseline_summary,
                "baseline_suggested_action": job.baseline_suggested_action,
            },
        )
        try:
            case = build_shadow_case(job)
            extraction_fn = extractor or run_fact_extraction
            extraction, call_metadata = extraction_fn(
                case,
                model_override=model_override or None,
                timeout_sec=max(1, int(timeout_seconds)),
            )
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
            unsafe_update = _candidate_is_unsafe(
                recommendation,
                evidence,
                expected_project_id=job.project_id,
            )
            review_required = bool(
                recommendation.proposed_fields or recommendation.review_reasons
            )
            candidate_fields = {
                str(name): str(value)
                for name, value in recommendation.proposed_fields.items()
            }
            comparison = _comparison_classification(
                job.baseline_status,
                baseline_fields,
                candidate_fields,
            )
            record = EmailProjectTruthShadowRecord(
                project_id=job.project_id,
                source_ref=job.source_ref,
                baseline_status=job.baseline_status,
                candidate_status="completed",
                baseline_fields=baseline_fields,
                candidate_fields=candidate_fields,
                citation_coverage=recommendation.state_change_citation_coverage,
                unsafe_update=unsafe_update,
                review_required=review_required,
                review_reason_count=len(recommendation.review_reasons),
                accepted_citations=evidence.accepted_citations,
                total_citations=evidence.total_citations,
                rejected_citations=len(evidence.rejected_citations),
                model=call_metadata.model or None,
                latency_ms=call_metadata.latency_ms,
                queue_lag_ms=queue_lag_ms,
                parent_trace_id=job.parent_trace_id,
                parent_run_id=job.parent_run_id,
                shadow_trace_id=trace_context.trace_id,
                shadow_run_id=trace_context.run_id,
            )
            persisted = _persist_safely(persist_fn, record)
            observation = EmailProjectTruthShadowObservation(
                execution_status="completed",
                project_id=job.project_id,
                comparison_classification=comparison,
                elapsed_ms=(time.monotonic() - started) * 1000,
                extraction_ms=call_metadata.latency_ms,
                queue_lag_ms=queue_lag_ms,
                model=call_metadata.model or None,
                baseline_fields=tuple(sorted(baseline_fields)),
                candidate_fields=tuple(sorted(candidate_fields)),
                citation_coverage=recommendation.state_change_citation_coverage,
                unsafe_update=unsafe_update,
                review_required=review_required,
                accepted_citations=evidence.accepted_citations,
                total_citations=evidence.total_citations,
                rejected_citations=len(evidence.rejected_citations),
                persisted=persisted,
                shadow_trace_id=trace_context.trace_id,
                shadow_run_id=trace_context.run_id,
            )
            _safe_event(
                trace_context,
                "email_project_truth.shadow_observation",
                status="completed" if persisted else "degraded",
                duration_ms=observation.elapsed_ms,
                metadata={**observation.safe_metadata(), "parent_run_id": job.parent_run_id},
                payload={
                    "case": case.model_dump(mode="json"),
                    "validated_evidence": evidence.model_dump(mode="json"),
                    "candidate_recommendation": recommendation.model_dump(mode="json"),
                    "baseline_fields": baseline_fields,
                },
            )
            _safe_run_finish(
                trace_context,
                status="completed",
                exit_reason="completed",
                persistence_status="completed" if persisted else "degraded",
                metadata={**observation.safe_metadata(), "parent_run_id": job.parent_run_id},
            )
            return observation
        except Exception as exc:
            elapsed_ms = (time.monotonic() - started) * 1000
            record = EmailProjectTruthShadowRecord(
                project_id=job.project_id,
                source_ref=job.source_ref,
                baseline_status=job.baseline_status,
                candidate_status="failed",
                baseline_fields=baseline_fields,
                queue_lag_ms=queue_lag_ms,
                parent_trace_id=job.parent_trace_id,
                parent_run_id=job.parent_run_id,
                shadow_trace_id=trace_context.trace_id,
                shadow_run_id=trace_context.run_id,
                error_category=type(exc).__name__,
            )
            persisted = _persist_safely(persist_fn, record)
            observation = EmailProjectTruthShadowObservation(
                execution_status="failed",
                project_id=job.project_id,
                comparison_classification="candidate_error",
                elapsed_ms=elapsed_ms,
                queue_lag_ms=queue_lag_ms,
                baseline_fields=tuple(sorted(baseline_fields)),
                persisted=persisted,
                error_class=type(exc).__name__,
                shadow_trace_id=trace_context.trace_id,
                shadow_run_id=trace_context.run_id,
            )
            _safe_event(
                trace_context,
                "email_project_truth.shadow_observation",
                status="error",
                duration_ms=elapsed_ms,
                metadata={**observation.safe_metadata(), "parent_run_id": job.parent_run_id},
                payload={"error": str(exc), "source_ref": job.source_ref},
            )
            _safe_run_finish(
                trace_context,
                status="error",
                exit_reason=type(exc).__name__,
                persistence_status="completed" if persisted else "error",
                anomaly_flags=["error"],
                metadata={**observation.safe_metadata(), "parent_run_id": job.parent_run_id},
            )
            return observation


def build_shadow_case(job: EmailProjectTruthShadowJob) -> EmailProjectTruthCase:
    newest = clean_email_body(job.body[:_MAX_BODY_CHARS])
    from src.email.commitment_dates import authored_datetime
    received_at = _parse_datetime(job.received_at)
    sources: list[EvidenceDocument] = [
        EvidenceDocument(
            source_type=EvidenceSourceType.EMAIL,
            source_ref=job.source_ref,
            text=newest,
            eligible_current_evidence=bool(newest),
            source_role="newest_authored_email",
            evidence_level=(job.source_authority.evidence_level if job.source_authority else EvidenceLevel.L5_UNVERIFIED_OR_INDIRECT),
            actor=(job.source_authority.actor if job.source_authority else job.sender_email or None),
            event_at=received_at,
        )
    ]
    partial_attachments: list[str] = []
    sources.extend(_attachment_sources(job, received_at, partial=partial_attachments))
    return EmailProjectTruthCase(
        case_id=f"SHADOW-{_hash_text(job.source_ref)[:16]}",
        source_ref=job.source_ref,
        project_id=job.project_id,
        subject=job.subject,
        sender_email=job.sender_email,
        received_at=received_at,
        authored_at=authored_datetime(job.sent_at),
        newest_authored_body=newest,
        sources=sources,
        project_snapshot=job.project_snapshot.model_copy(deep=True),
        project_identity_resolved_hint=True,
        quoted_history=not bool(newest) and bool(job.body.strip()),
        forwarded_attachment=bool(job.attachments),
        partial_attachment_names=partial_attachments,
        unread_attachment_names=[
            item.filename for index, item in enumerate(job.attachments, start=1)
            if f"attachment:{job.source_ref}:{index}" not in {source.source_ref for source in sources}
        ],
        synthetic=False,
        category="production_shadow",
    )


def _attachment_sources(
    job: EmailProjectTruthShadowJob,
    received_at: datetime | None,
    *, partial: list[str] | None = None,
) -> list[EvidenceDocument]:
    if not job.attachments:
        return []
    from src.email.attachment_extractor import can_extract, extract_text_from_bytes

    sources: list[EvidenceDocument] = []
    total = 0
    for index, attachment in enumerate(job.attachments, start=1):
        if total >= _MAX_ATTACHMENT_CHARS_TOTAL:
            break
        if not attachment.data or not can_extract(
            attachment.content_type,
            attachment.filename,
        ):
            continue
        try:
            extracted = extract_text_from_bytes(
                attachment.data,
                attachment.content_type,
                attachment.filename,
            )
        except Exception:
            continue
        if not extracted.get("success") or not extracted.get("text"):
            continue
        budget = min(
            _MAX_ATTACHMENT_CHARS_PER_FILE,
            _MAX_ATTACHMENT_CHARS_TOTAL - total,
        )
        full_text = str(extracted.get("text") or "").strip()
        text = full_text[:budget]
        if len(full_text) > budget and partial is not None:
            partial.append(attachment.filename)
        if not text:
            continue
        total += len(text)
        sources.append(
            EvidenceDocument(
                source_type=EvidenceSourceType.ATTACHMENT,
                source_ref=f"attachment:{job.source_ref}:{index}",
                text=text,
                eligible_current_evidence=True,
                source_role="attachment",
                evidence_level=EvidenceLevel.L5_UNVERIFIED_OR_INDIRECT,
                actor=job.sender_email or None,
                event_at=received_at,
            )
        )
    return sources


def _candidate_is_unsafe(
    recommendation: EmailProjectRecommendation,
    evidence: Any,
    *,
    expected_project_id: str,
) -> bool:
    proposed = recommendation.proposed_fields
    if not proposed:
        return False
    if set(proposed) - _ALLOWED_PROJECT_FIELDS:
        return True
    if not evidence.project_identity_resolved:
        return True
    if evidence.resolved_project_id != expected_project_id:
        return True
    if recommendation.assessment.project_id != expected_project_id:
        return True
    if recommendation.assessment.contradictions:
        return True
    return recommendation.state_change_citation_coverage < 1.0


def _comparison_classification(
    baseline_status: str,
    baseline_fields: Mapping[str, str],
    candidate_fields: Mapping[str, str],
) -> str:
    if baseline_status != "completed":
        return "baseline_error"
    if dict(baseline_fields) == dict(candidate_fields):
        return "exact"
    if set(baseline_fields) == set(candidate_fields):
        return "same_fields_different_values"
    if not baseline_fields and candidate_fields:
        return "candidate_only"
    if baseline_fields and not candidate_fields:
        return "baseline_only"
    return "different_fields"


def normalize_shadow_fields(fields: Mapping[str, Any] | None) -> tuple[tuple[str, str], ...]:
    return tuple(
        sorted(
            (str(name), str(value))
            for name, value in (fields or {}).items()
        )
    )


def _parse_datetime(value: str | None) -> datetime | None:
    if not value:
        return None
    normalized = value.strip().replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=timezone.utc)


def _hash_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _persist_safely(
    persist: Persist,
    record: EmailProjectTruthShadowRecord,
) -> bool:
    try:
        return persist(record) is not None
    except Exception:
        return False


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
    "EMAIL_PROJECT_TRUTH_SHADOW_VERSION",
    "EmailProjectTruthShadowAttachment",
    "EmailProjectTruthShadowJob",
    "EmailProjectTruthShadowObservation",
    "build_shadow_case",
    "normalize_shadow_fields",
    "observe_email_project_truth_shadow",
]
