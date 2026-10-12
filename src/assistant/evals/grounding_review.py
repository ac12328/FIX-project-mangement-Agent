"""Secure local workflow for blinded grounding-repair human review."""

from __future__ import annotations

import json
import secrets
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from src.assistant.evals.artifacts import (
    read_model,
    stamp_content_hash,
    verify_content_hash,
    write_model,
)
from src.assistant.evals.grounding_repair import (
    GroundingRepairCaseReview,
    GroundingRepairReviewBatch,
    GroundingRepairShadowCohort,
    ReviewStatus,
)
from src.assistant.evals.grounding_review_models import (
    CONTROLLED_SIMULATION_COLLECTION_CLASS,
    ISSUE_CODES,
    PASS_THRESHOLD,
    RUBRIC_DIMENSIONS,
    RUBRIC_ID,
    BlindAssignment,
    GroundingReviewRubric,
    ReviewAuditEntry,
    ReviewAuditLedger,
    ReviewCaseView,
    SubmissionResult,
    answer_hash,
    blinded_assignment,
    build_grounding_review_rubric,
    score_answer,
    validate_review_lineage,
)
from src.observability.payload_crypto import decrypt_payload


class GroundingReviewWorkflow:
    """Read encrypted evidence and persist only plaintext-free review decisions."""

    def __init__(
        self,
        *,
        db_path: Path,
        cohort_path: Path,
        pending_path: Path,
        progress_path: Path,
        rubric_path: Path,
        audit_path: Path,
        reviewer: str,
        session_secret: str | None = None,
    ) -> None:
        self.db_path = Path(db_path).resolve()
        self.cohort_path = Path(cohort_path).resolve()
        self.pending_path = Path(pending_path).resolve()
        self.progress_path = Path(progress_path).resolve()
        self.rubric_path = Path(rubric_path).resolve()
        self.audit_path = Path(audit_path).resolve()
        self.reviewer = reviewer.strip()
        self.session_secret = session_secret or secrets.token_urlsafe(32)
        self._lock = threading.RLock()
        if not self.reviewer:
            raise ValueError("reviewer is required")
        if not self.db_path.is_file():
            raise FileNotFoundError(self.db_path)
        if self.pending_path == self.progress_path:
            raise ValueError("progress ledger must be separate from pending ledger")

        self.cohort = read_model(self.cohort_path, GroundingRepairShadowCohort)
        self.rubric = read_model(self.rubric_path, GroundingReviewRubric)
        if not verify_content_hash(self.rubric):
            raise ValueError("rubric content hash is invalid")
        if (
            self.rubric.cohort_id != self.cohort.cohort_id
            or self.rubric.cohort_hash != self.cohort.content_hash
        ):
            raise ValueError("rubric does not reference this cohort")
        self._observations = {item.case_id: item for item in self.cohort.observations}
        self._batch = self._load_or_initialize_batch()
        self._audit = self._load_or_initialize_audit()

    def _load_or_initialize_batch(self) -> GroundingRepairReviewBatch:
        source = self.progress_path if self.progress_path.exists() else self.pending_path
        batch = read_model(source, GroundingRepairReviewBatch)
        validate_review_lineage(self.cohort, batch)
        self._validate_completed_reviews(batch)
        if source == self.pending_path:
            write_model(self.progress_path, batch)
        return batch

    def _validate_completed_reviews(self, batch: GroundingRepairReviewBatch) -> None:
        expected_keys = {
            "rubric_id", "rubric_hash", "candidate_dimensions", "baseline_dimensions"
        }
        for review in batch.reviews:
            if review.status != ReviewStatus.REVIEWED:
                continue
            try:
                notes = json.loads(review.notes)
            except (TypeError, ValueError) as exc:
                raise ValueError(f"invalid structured notes for {review.case_id}") from exc
            if not isinstance(notes, dict) or set(notes) != expected_keys:
                raise ValueError(f"invalid structured notes for {review.case_id}")
            if (
                notes.get("rubric_id") != RUBRIC_ID
                or notes.get("rubric_hash") != self.rubric.content_hash
            ):
                raise ValueError(f"rubric lineage mismatch for {review.case_id}")
            candidate = score_answer(
                dict(notes["candidate_dimensions"]), review.candidate_issue_codes
            )
            baseline = score_answer(
                dict(notes["baseline_dimensions"]), review.baseline_issue_codes
            )
            if (
                candidate.total != review.candidate_score
                or candidate.passed != review.candidate_pass
                or baseline.total != review.baseline_score
                or baseline.passed != review.baseline_pass
            ):
                raise ValueError(f"review score mismatch for {review.case_id}")

    def _load_or_initialize_audit(self) -> ReviewAuditLedger:
        if self.audit_path.exists():
            audit = read_model(self.audit_path, ReviewAuditLedger)
            if not verify_content_hash(audit):
                raise ValueError("review audit content hash is invalid")
            if (
                audit.cohort_id != self.cohort.cohort_id
                or audit.cohort_hash != self.cohort.content_hash
                or audit.rubric_hash != self.rubric.content_hash
            ):
                raise ValueError("review audit lineage mismatch")
            return audit
        audit = stamp_content_hash(ReviewAuditLedger(
            cohort_id=self.cohort.cohort_id,
            cohort_hash=self.cohort.content_hash,
            rubric_hash=self.rubric.content_hash,
            created_at=datetime.now(timezone.utc),
        ))
        write_model(self.audit_path, audit)
        return audit

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self.db_path), timeout=5, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA query_only = ON")
        return conn

    def _append_audit(self, action: str, *, case_id: str, run_id: str) -> None:
        entry = ReviewAuditEntry(
            action=action,
            occurred_at=datetime.now(timezone.utc),
            reviewer=self.reviewer,
            case_id=case_id,
            run_id=run_id,
            event_id=case_id,
        )
        self._audit = stamp_content_hash(self._audit.model_copy(update={
            "entries": [*self._audit.entries, entry],
            "content_hash": "",
        }))
        write_model(self.audit_path, self._audit)

    def status(self) -> dict[str, int]:
        with self._lock:
            reviewed = sum(item.status == ReviewStatus.REVIEWED for item in self._batch.reviews)
            return {
                "total": len(self._batch.reviews),
                "reviewed": reviewed,
                "pending": len(self._batch.reviews) - reviewed,
            }

    def _next_review(self) -> GroundingRepairCaseReview | None:
        return next(
            (item for item in self._batch.reviews if item.status == ReviewStatus.PENDING),
            None,
        )

    def _load_case_payload(
        self, review: GroundingRepairCaseReview
    ) -> tuple[dict[str, Any], str, str]:
        observation = self._observations[review.case_id]
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM trace_events WHERE event_id=?", (review.case_id,)
            ).fetchone()
            if row is None or row["event_type"] != "grounding.repair.shadow":
                raise ValueError(f"grounding shadow event unavailable for {review.case_id}")
            if str(row["run_id"]) != observation.run_id:
                raise ValueError(f"run lineage mismatch for {review.case_id}")
            if not row["payload_ciphertext"] or not row["payload_nonce"]:
                raise ValueError(f"encrypted payload unavailable for {review.case_id}")
            payload = decrypt_payload(
                row["payload_ciphertext"],
                row["payload_nonce"],
                row["payload_key_version"] or "v1",
            )
            start = conn.execute(
                "SELECT * FROM trace_events WHERE run_id=? AND event_type='run.start' "
                "ORDER BY sequence LIMIT 1",
                (observation.run_id,),
            ).fetchone()
        if not isinstance(payload, dict):
            raise ValueError(f"invalid grounding payload for {review.case_id}")
        candidate = payload.get("candidate_answer")
        baseline = payload.get("baseline_answer")
        prepared = payload.get("prepared_answer")
        if not all(isinstance(item, str) for item in (candidate, baseline, prepared)):
            raise ValueError(f"answer payload is incomplete for {review.case_id}")
        for label, value, expected in (
            ("candidate", candidate, review.candidate_answer_hash),
            ("baseline", baseline, review.baseline_answer_hash),
            ("prepared", prepared, observation.prepared_answer_hash),
        ):
            if expected and answer_hash(value) != expected:
                raise ValueError(f"{label} answer hash mismatch for {review.case_id}")
        request_text = ""
        if start is not None and start["payload_ciphertext"] and start["payload_nonce"]:
            start_payload = decrypt_payload(
                start["payload_ciphertext"],
                start["payload_nonce"],
                start["payload_key_version"] or "v1",
            )
            if isinstance(start_payload, dict) and isinstance(
                start_payload.get("user_input"), str
            ):
                request_text = start_payload["user_input"]
        return payload, request_text, observation.run_id

    def verify_all_case_payloads(self) -> int:
        with self._lock:
            for review in self._batch.reviews:
                self._load_case_payload(review)
            return len(self._batch.reviews)

    def next_case(self) -> ReviewCaseView | None:
        with self._lock:
            review = self._next_review()
            if review is None:
                return None
            payload, request_text, run_id = self._load_case_payload(review)
            assignment = blinded_assignment(review.case_id, self.session_secret)
            candidate = str(payload["candidate_answer"])
            baseline = str(payload["baseline_answer"])
            answer_a, answer_b = (
                (candidate, baseline)
                if assignment == BlindAssignment.CANDIDATE_IS_A
                else (baseline, candidate)
            )
            completed = sum(
                item.status == ReviewStatus.REVIEWED for item in self._batch.reviews
            )
            self._append_audit("case_revealed", case_id=review.case_id, run_id=run_id)
            return ReviewCaseView(
                case_id=review.case_id,
                run_id=run_id,
                position=completed + 1,
                total=len(self._batch.reviews),
                completed=completed,
                user_request=request_text,
                prepared_answer=str(payload["prepared_answer"]),
                answer_a=answer_a,
                answer_b=answer_b,
                sources=list(payload.get("sources") or []),
                tool_results=list(payload.get("tool_results") or []),
                initial_errors=[str(item) for item in payload.get("initial_errors") or []],
            )

    def submit(
        self,
        *,
        case_id: str,
        answer_a_dimensions: dict[str, Any],
        answer_b_dimensions: dict[str, Any],
        answer_a_issue_codes: list[str],
        answer_b_issue_codes: list[str],
    ) -> SubmissionResult:
        with self._lock:
            review = self._next_review()
            if review is None:
                raise ValueError("all review cases are complete")
            if not secrets.compare_digest(review.case_id, str(case_id)):
                raise ValueError("submission does not match the next pending case")
            score_a = score_answer(answer_a_dimensions, answer_a_issue_codes)
            score_b = score_answer(answer_b_dimensions, answer_b_issue_codes)
            assignment = blinded_assignment(review.case_id, self.session_secret)
            candidate, baseline = (
                (score_a, score_b)
                if assignment == BlindAssignment.CANDIDATE_IS_A
                else (score_b, score_a)
            )
            notes = json.dumps(
                {
                    "rubric_id": RUBRIC_ID,
                    "rubric_hash": self.rubric.content_hash,
                    "candidate_dimensions": candidate.dimensions,
                    "baseline_dimensions": baseline.dimensions,
                },
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            )
            updated = GroundingRepairCaseReview(
                case_id=review.case_id,
                candidate_answer_hash=review.candidate_answer_hash,
                baseline_answer_hash=review.baseline_answer_hash,
                status=ReviewStatus.REVIEWED,
                reviewer=self.reviewer,
                reviewed_at=datetime.now(timezone.utc),
                candidate_score=candidate.total,
                baseline_score=baseline.total,
                candidate_pass=candidate.passed,
                baseline_pass=baseline.passed,
                candidate_issue_codes=candidate.issue_codes,
                baseline_issue_codes=baseline.issue_codes,
                notes=notes,
            )
            reviews = [
                updated if item.case_id == review.case_id else item
                for item in self._batch.reviews
            ]
            candidate_batch = GroundingRepairReviewBatch.model_validate({
                **self._batch.model_dump(mode="python"),
                "reviews": reviews,
                "content_hash": "",
            })
            candidate_batch = stamp_content_hash(candidate_batch)
            validate_review_lineage(self.cohort, candidate_batch)
            write_model(self.progress_path, candidate_batch)
            self._batch = candidate_batch
            self._append_audit(
                "review_submitted",
                case_id=review.case_id,
                run_id=self._observations[review.case_id].run_id,
            )
            return SubmissionResult(
                case_id=review.case_id,
                assignment=assignment,
                answer_a=score_a,
                answer_b=score_b,
                candidate=candidate,
                baseline=baseline,
            )


__all__ = [
    "BlindAssignment",
    "CONTROLLED_SIMULATION_COLLECTION_CLASS",
    "GroundingReviewRubric",
    "GroundingReviewWorkflow",
    "ISSUE_CODES",
    "PASS_THRESHOLD",
    "RUBRIC_DIMENSIONS",
    "RUBRIC_ID",
    "ReviewCaseView",
    "SubmissionResult",
    "blinded_assignment",
    "build_grounding_review_rubric",
    "score_answer",
    "validate_review_lineage",
]
