"""Independent metadata-only diagnostics for incomplete Policy-v4 reviews.

This module never persists decrypted questions, answers, reference responses, sources,
rationales, or feature rows. It reruns only the incomplete source-review worklist and
records hashes, scores, issue/error codes, retry counts, and timing metadata.
"""

from __future__ import annotations

import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Literal

from pydantic import Field, model_validator

from src.assistant.evals.artifacts import read_model, write_model
from src.assistant.evals.grounding_repair_v4_protocol import (
    AutomatedCaseReview,
    AutomatedReviewBatch,
    HoldoutCollection,
    Preregistration,
    SelectionManifest,
    V4Model,
    hash_identity,
    stamp_v4,
    verify_v4,
)
from src.assistant.evals.grounding_repair_v4_review_runner import (
    review_worklist_resumable_v4,
)

_SAFE_CODE_RE = re.compile(r"^[A-Za-z0-9_.:+-]+$")


class ReviewPosthocDiagnostic(V4Model):
    """Sealed, plaintext-free result of rerunning an incomplete review subset."""

    schema_version: Literal[1] = 1
    diagnostic_id: str = Field(min_length=3)
    source_experiment_id: str = Field(min_length=1)
    started_at: datetime
    completed_at: datetime
    source_holdout_hash: str = Field(min_length=8)
    source_selection_hash: str = Field(min_length=8)
    source_review_hash: str = Field(min_length=8)
    model_identity_hash: str = Field(min_length=8)
    worklist_hash: str = Field(min_length=8)
    preflight_execution_model_hash: str = Field(min_length=8)
    source_incomplete_count: int = Field(ge=0)
    review_count: int = Field(ge=0)
    complete_count: int = Field(ge=0)
    incomplete_count: int = Field(ge=0)
    completion_rate: float = Field(ge=0, le=1)
    content_attempt_count: int = Field(ge=0)
    transport_retry_count: int = Field(ge=0)
    source_error_clusters: dict[str, int]
    diagnostic_error_clusters: dict[str, int]
    status: Literal["in_progress", "complete", "incomplete"]
    reviews: list[AutomatedCaseReview]
    content_hash: str = ""

    @model_validator(mode="after")
    def validate_counts(self) -> "ReviewPosthocDiagnostic":
        if self.review_count != len(self.reviews):
            raise ValueError("post-hoc review count mismatch")
        if self.complete_count + self.incomplete_count != self.review_count:
            raise ValueError("post-hoc completion counts mismatch")
        expected_rate = self.complete_count / self.source_incomplete_count \
            if self.source_incomplete_count else 1.0
        if abs(self.completion_rate - expected_rate) > 1e-12:
            raise ValueError("post-hoc completion rate mismatch")
        expected_status = (
            "complete" if self.review_count == self.source_incomplete_count
            and self.incomplete_count == 0 else
            "incomplete" if self.review_count == self.source_incomplete_count else
            "in_progress"
        )
        if self.status != expected_status:
            raise ValueError("post-hoc status mismatch")
        hashes = [item.question_hash for item in self.reviews]
        if len(hashes) != len(set(hashes)):
            raise ValueError("post-hoc reviews contain duplicate question hashes")
        return self


def _safe_code(value: str | None) -> str:
    code = str(value or "none")
    return code if _SAFE_CODE_RE.fullmatch(code) else "unsafe_error_code"


def _error_clusters(reviews: list[AutomatedCaseReview]) -> dict[str, int]:
    clusters: dict[str, int] = {}
    for item in reviews:
        code = "complete" if item.complete else _safe_code(item.error_code)
        clusters[code] = clusters.get(code, 0) + 1
    return dict(sorted(clusters.items()))


def _diagnostic(
    *, diagnostic_id: str, source_experiment_id: str, started_at: datetime,
    source_holdout_hash: str, source_selection_hash: str, source_review_hash: str,
    model_identity_hash: str, work: list[tuple[str, str]], preflight_model_hash: str,
    source_failures: list[AutomatedCaseReview], reviews: list[AutomatedCaseReview],
) -> ReviewPosthocDiagnostic:
    complete_count = sum(item.complete for item in reviews)
    incomplete_count = len(reviews) - complete_count
    source_count = len(work)
    status = (
        "complete" if len(reviews) == source_count and incomplete_count == 0 else
        "incomplete" if len(reviews) == source_count else
        "in_progress"
    )
    return stamp_v4(ReviewPosthocDiagnostic(
        diagnostic_id=diagnostic_id,
        source_experiment_id=source_experiment_id,
        started_at=started_at,
        completed_at=datetime.now(timezone.utc),
        source_holdout_hash=source_holdout_hash,
        source_selection_hash=source_selection_hash,
        source_review_hash=source_review_hash,
        model_identity_hash=model_identity_hash,
        worklist_hash=hash_identity(work),
        preflight_execution_model_hash=preflight_model_hash,
        source_incomplete_count=source_count,
        review_count=len(reviews),
        complete_count=complete_count,
        incomplete_count=incomplete_count,
        completion_rate=(complete_count / source_count if source_count else 1.0),
        content_attempt_count=sum(item.attempt_count for item in reviews),
        transport_retry_count=sum(item.transport_retry_count for item in reviews),
        source_error_clusters=_error_clusters(source_failures),
        diagnostic_error_clusters=_error_clusters(reviews),
        status=status,
        reviews=reviews,
    ))


def _validate_source(
    holdout: HoldoutCollection, selection: SelectionManifest,
    source_reviews: AutomatedReviewBatch, preregistration: Preregistration,
) -> list[AutomatedCaseReview]:
    if not all(verify_v4(item) for item in (holdout, selection, source_reviews, preregistration)):
        raise ValueError("post-hoc source artifact hash is invalid")
    if selection.holdout_hash != holdout.content_hash:
        raise ValueError("post-hoc holdout lineage mismatch")
    if source_reviews.selection_hash != selection.content_hash:
        raise ValueError("post-hoc source review lineage mismatch")
    if source_reviews.experiment_id != selection.experiment_id:
        raise ValueError("post-hoc source experiment mismatch")
    if source_reviews.review_count != selection.selected_case_count:
        raise ValueError("post-hoc source review count mismatch")
    for index, item in enumerate(source_reviews.reviews):
        expected = (
            selection.selected_question_hashes[index],
            selection.selected_case_payload_hashes[index],
        )
        if (item.question_hash, item.case_payload_hash) != expected:
            raise ValueError("post-hoc source review order mismatch")
        if item.model_identity_hash != preregistration.model_identity_hash:
            raise ValueError("post-hoc source review model identity mismatch")
    return [item for item in source_reviews.reviews if not item.complete]


def run_review_posthoc_diagnostic_v4(
    *, diagnostic_id: str, holdout_path: Path, selection_path: Path,
    source_reviews_path: Path, preregistration_path: Path, output_path: Path,
    preflight: Callable[[], str], attempts: int = 3, concurrency: int = 4,
    resume: bool = True, call: Any | None = None, circuit_pause_seconds: float = 15.0,
) -> ReviewPosthocDiagnostic:
    """Rerun only incomplete source reviews after a successful evaluator preflight."""
    holdout = read_model(holdout_path, HoldoutCollection)
    selection = read_model(selection_path, SelectionManifest)
    source_reviews = read_model(source_reviews_path, AutomatedReviewBatch)
    preregistration = read_model(preregistration_path, Preregistration)
    source_failures = _validate_source(holdout, selection, source_reviews, preregistration)
    work = [(item.question_hash, item.case_payload_hash) for item in source_failures]
    worklist_hash = hash_identity(work)
    output_path = Path(output_path)
    started_at = datetime.now(timezone.utc)
    existing_reviews: list[AutomatedCaseReview] = []
    preflight_model_hash = "sha256:" + "0" * 64
    if output_path.exists():
        if not resume:
            raise FileExistsError(output_path)
        existing = read_model(output_path, ReviewPosthocDiagnostic)
        if not verify_v4(existing):
            raise ValueError("post-hoc checkpoint hash is invalid")
        expected_lineage = (
            diagnostic_id, selection.experiment_id, holdout.content_hash,
            selection.content_hash, source_reviews.content_hash,
            preregistration.model_identity_hash, worklist_hash,
        )
        actual_lineage = (
            existing.diagnostic_id, existing.source_experiment_id,
            existing.source_holdout_hash, existing.source_selection_hash,
            existing.source_review_hash, existing.model_identity_hash, existing.worklist_hash,
        )
        if actual_lineage != expected_lineage:
            raise ValueError("post-hoc checkpoint lineage mismatch")
        started_at = existing.started_at
        existing_reviews = list(existing.reviews)
        preflight_model_hash = existing.preflight_execution_model_hash
        if existing.status == "complete":
            return existing

    preflight_model = preflight()
    preflight_model_hash = hash_identity({"execution_model": preflight_model})

    def checkpoint(reviews: list[AutomatedCaseReview]) -> None:
        write_model(output_path, _diagnostic(
            diagnostic_id=diagnostic_id,
            source_experiment_id=selection.experiment_id,
            started_at=started_at,
            source_holdout_hash=holdout.content_hash,
            source_selection_hash=selection.content_hash,
            source_review_hash=source_reviews.content_hash,
            model_identity_hash=preregistration.model_identity_hash,
            work=work,
            preflight_model_hash=preflight_model_hash,
            source_failures=source_failures,
            reviews=reviews,
        ))

    reviews = review_worklist_resumable_v4(
        work=work,
        selected_cases=holdout.cases,
        model_identity_hash=preregistration.model_identity_hash,
        existing_reviews=existing_reviews,
        attempts=attempts,
        concurrency=concurrency,
        call=call,
        preflight=preflight,
        circuit_pause_seconds=circuit_pause_seconds,
        checkpoint=checkpoint,
    )
    result = _diagnostic(
        diagnostic_id=diagnostic_id,
        source_experiment_id=selection.experiment_id,
        started_at=started_at,
        source_holdout_hash=holdout.content_hash,
        source_selection_hash=selection.content_hash,
        source_review_hash=source_reviews.content_hash,
        model_identity_hash=preregistration.model_identity_hash,
        work=work,
        preflight_model_hash=preflight_model_hash,
        source_failures=source_failures,
        reviews=reviews,
    )
    write_model(output_path, result)
    return result


__all__ = ["ReviewPosthocDiagnostic", "run_review_posthoc_diagnostic_v4"]
