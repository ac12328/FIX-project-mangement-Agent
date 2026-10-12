"""Contracts and encrypted checkpoint helpers for Policy-v7 reviews.

The evaluator reuses the independent Reference-R and blinded A/B judge from the
sealed Policy-v4 workflow, but replaces only the candidate answer in memory with
the deterministic Policy-v7 canonical source projection. Checkpoints encrypt every per-case
record; durable plaintext contains aggregate progress and artifact lineage only.
"""

from __future__ import annotations

import hashlib
import json
import re
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Literal

from pydantic import Field, model_validator

from src.assistant.evals.artifacts import read_model, write_model
from src.assistant.evals.grounding_repair_candidate_v7 import (
    POLICY_VERSION_V7,
    build_grounding_repair_candidate_v7,
)
from src.assistant.evals.grounding_repair_v4_protocol import (
    AutomatedCaseReview,
    AutomatedReviewBatch,
    EncryptedBlob,
    HoldoutCase,
    HoldoutCollection,
    Preregistration,
    SelectionManifest,
    V4Model,
    hash_identity,
    stamp_v4,
    verify_v4,
)
from src.assistant.evals.grounding_repair_v4_review import (
    prompt_hashes_v4,
    review_case_v4,
)
from src.assistant.evals.grounding_repair_v4_review_transport import (
    TransportExhaustedError,
)
from src.observability.payload_crypto import decrypt_payload, encrypt_payload

TRANSFORMED_REVIEW_PROTOCOL_V7 = "policy-v7-transformed-reference-r-blinded-review-v1"
_SAFE_CODE_RE = re.compile(r"^[A-Za-z0-9_.:+-]+$")
ReviewerV7 = Callable[..., AutomatedCaseReview]


class TransformedReviewRecordV7(V4Model):
    """Encrypted-only record linking one source case to its transformed review."""

    source_question_hash: str = Field(min_length=8)
    source_case_payload_hash: str = Field(min_length=8)
    transformed_case_payload_hash: str = Field(min_length=8)
    transformed_candidate_hash: str = Field(min_length=8)
    review: AutomatedCaseReview

    @model_validator(mode="after")
    def validate_review_lineage(self) -> "TransformedReviewRecordV7":
        if self.review.question_hash != self.source_question_hash:
            raise ValueError("transformed review question hash mismatch")
        if self.review.case_payload_hash != self.transformed_case_payload_hash:
            raise ValueError("transformed review payload hash mismatch")
        return self


class TransformedReviewCheckpointPayloadV7(V4Model):
    """Sensitive checkpoint payload that is encrypted before persistence."""

    schema_version: Literal[1] = 1
    experiment_id: str = Field(min_length=3)
    protocol: Literal[TRANSFORMED_REVIEW_PROTOCOL_V7] = TRANSFORMED_REVIEW_PROTOCOL_V7
    source_selection_hash: str = Field(min_length=8)
    transformed_model_identity_hash: str = Field(min_length=8)
    review_scope: Literal[
        "all_selected_v4_cases", "policy_v7_certificate_selected_cases"
    ]
    review_target_count: int = Field(ge=0)
    records: list[TransformedReviewRecordV7]


class TransformedReviewArtifactV7(V4Model):
    """Aggregate-only plaintext envelope for encrypted transformed reviews."""

    schema_version: Literal[1] = 1
    experiment_id: str = Field(min_length=3)
    generated_at: datetime
    updated_at: datetime
    status: Literal["in_progress", "complete"]
    policy_version: Literal[POLICY_VERSION_V7] = POLICY_VERSION_V7
    review_protocol: Literal[TRANSFORMED_REVIEW_PROTOCOL_V7] = TRANSFORMED_REVIEW_PROTOCOL_V7
    source_holdout_hash: str = Field(min_length=8)
    source_selection_hash: str = Field(min_length=8)
    source_review_hash: str = Field(min_length=8)
    source_model_identity_hash: str = Field(min_length=8)
    transformed_model_identity_hash: str = Field(min_length=8)
    transformed_reference_prompt_identity_hashes: list[str] = Field(min_length=1)
    transformed_judge_prompt_identity_hash: str = Field(min_length=8)
    transform_implementation_hash: str = Field(min_length=8)
    review_scope: Literal[
        "all_selected_v4_cases", "policy_v7_certificate_selected_cases"
    ]
    source_selection_case_count: int = Field(ge=0)
    review_target_count: int = Field(ge=0)
    review_count: int = Field(ge=0)
    complete_review_count: int = Field(ge=0)
    review_coverage: float = Field(ge=0, le=1)
    candidate_issue_count: int = Field(ge=0)
    incomplete_error_code_counts: dict[str, int]
    encrypted_checkpoint: EncryptedBlob
    research_only: Literal[True] = True
    serving_change_authorized: Literal[False] = False
    content_hash: str = ""

    @model_validator(mode="after")
    def validate_aggregate_counts(self) -> "TransformedReviewArtifactV7":
        if self.review_count > self.review_target_count:
            raise ValueError("transformed review count exceeds target")
        if self.complete_review_count > self.review_count:
            raise ValueError("complete transformed review count exceeds review count")
        expected = self.complete_review_count / self.review_target_count if self.review_target_count else 0.0
        if abs(self.review_coverage - expected) > 1e-12:
            raise ValueError("transformed review coverage mismatch")
        if self.status == "complete" and self.review_count != self.review_target_count:
            raise ValueError("complete transformed review artifact has incomplete worklist")
        if any(value < 0 for value in self.incomplete_error_code_counts.values()):
            raise ValueError("transformed review error counts cannot be negative")
        return self


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _payload_hash(value: Any) -> str:
    encoded = json.dumps(value, ensure_ascii=False, default=str).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _safe_code(value: str | None) -> str:
    code = str(value or "none")
    return code if _SAFE_CODE_RE.fullmatch(code) else "unsafe_code"


def transformed_review_identity_v7(preregistration: Preregistration) -> str:
    """Bind the transformed evaluator identity to source routing and v7 protocol."""

    return hash_identity({
        "protocol": TRANSFORMED_REVIEW_PROTOCOL_V7,
        "policy_version": POLICY_VERSION_V7,
        "source_model_identity_hash": preregistration.model_identity_hash,
        "transform_implementation_hash": transform_implementation_hash_v7(),
        "prompt_identity": transformed_prompt_identities_v7(),
    })


def transformed_prompt_identities_v7() -> tuple[list[str], str]:
    """Return distinct lineage identities for the reused independent evaluator."""

    reference_hashes, judge_hash = prompt_hashes_v4()
    transformed_reference = [
        hash_identity({
            "protocol": TRANSFORMED_REVIEW_PROTOCOL_V7,
            "role": "reference_r",
            "source_prompt_hash": value,
        })
        for value in reference_hashes
    ]
    transformed_judge = hash_identity({
        "protocol": TRANSFORMED_REVIEW_PROTOCOL_V7,
        "role": "blinded_ab_judge",
        "source_prompt_hash": judge_hash,
        "candidate_semantics": "policy_v7_canonical_source_projection",
    })
    return transformed_reference, transformed_judge


def transform_implementation_hash_v7() -> str:
    """Hash the offline transformation implementation used by the review."""

    paths = (
        Path(__file__).with_name("grounding_repair_candidate_v7.py"),
        Path(__file__).with_name("grounding_repair_certificate_v7.py"),
    )
    digest = hashlib.sha256()
    for path in paths:
        digest.update(path.name.encode("utf-8"))
        digest.update(b"\0")
        digest.update(path.read_bytes())
        digest.update(b"\0")
    return "sha256:" + digest.hexdigest()


def _decrypt_source_case(case: HoldoutCase, expected_payload_hash: str) -> dict[str, Any]:
    blob = case.encrypted_case_payload
    if blob is None or case.case_payload_hash != expected_payload_hash:
        raise ValueError("transformed review source case payload is unavailable")
    payload = decrypt_payload(blob.ciphertext, blob.nonce, blob.key_version)
    if not isinstance(payload, dict):
        raise ValueError("transformed review source payload is not an object")
    if blob.payload_hash != expected_payload_hash or _payload_hash(payload) != expected_payload_hash:
        raise ValueError("transformed review source payload hash mismatch")
    if payload.get("question_hash") != case.question_hash:
        raise ValueError("transformed review source question lineage mismatch")
    return payload



def transformed_candidate_hash_v7(candidate_answer: str) -> str:
    """Return the identity used to correlate one in-memory transformed answer."""

    return hash_identity({
        "policy_version": POLICY_VERSION_V7,
        "candidate_answer": str(candidate_answer or ""),
    })


def transformed_case_payload_hash_v7(
    *, source_case_payload_hash: str, transformed_candidate_hash: str,
) -> str:
    """Return a derived review identity without persisting transformed plaintext."""

    return hash_identity({
        "protocol": TRANSFORMED_REVIEW_PROTOCOL_V7,
        "source_case_payload_hash": source_case_payload_hash,
        "transformed_candidate_hash": transformed_candidate_hash,
    })

def _transform_case_v7(
    payload: dict[str, Any],
) -> tuple[dict[str, Any], str, str]:
    sources = [item for item in payload.get("sources") or [] if isinstance(item, dict)]
    tool_results = [
        item for item in payload.get("tool_results") or [] if isinstance(item, dict)
    ]
    canonical = build_grounding_repair_candidate_v7(
        question=str(payload.get("question") or ""),
        sources=sources,
        tool_results=tool_results,
    )
    if not canonical.eligible:
        raise ValueError("transformed review case is not Policy-v7 eligible")
    transformed = dict(payload)
    transformed["candidate_answer"] = canonical.candidate_answer
    candidate_hash = transformed_candidate_hash_v7(canonical.candidate_answer)
    transformed_payload_hash = transformed_case_payload_hash_v7(
        source_case_payload_hash=_payload_hash(payload),
        transformed_candidate_hash=candidate_hash,
    )
    return transformed, transformed_payload_hash, candidate_hash


def _checkpoint_payload(
    *,
    experiment_id: str,
    source_selection_hash: str,
    transformed_model_identity_hash: str,
    review_scope: Literal[
        "all_selected_v4_cases", "policy_v7_certificate_selected_cases"
    ],
    review_target_count: int,
    records: list[TransformedReviewRecordV7],
) -> TransformedReviewCheckpointPayloadV7:
    return TransformedReviewCheckpointPayloadV7(
        experiment_id=experiment_id,
        source_selection_hash=source_selection_hash,
        transformed_model_identity_hash=transformed_model_identity_hash,
        review_scope=review_scope,
        review_target_count=review_target_count,
        records=records,
    )


def _artifact(
    *,
    experiment_id: str,
    generated_at: datetime,
    holdout: HoldoutCollection,
    selection: SelectionManifest,
    source_reviews: AutomatedReviewBatch,
    preregistration: Preregistration,
    transformed_model_identity_hash: str,
    records: list[TransformedReviewRecordV7],
    review_scope: Literal[
        "all_selected_v4_cases", "policy_v7_certificate_selected_cases"
    ],
    review_target_count: int,
) -> TransformedReviewArtifactV7:
    payload = _checkpoint_payload(
        experiment_id=experiment_id,
        source_selection_hash=selection.content_hash,
        transformed_model_identity_hash=transformed_model_identity_hash,
        review_scope=review_scope,
        review_target_count=review_target_count,
        records=records,
    )
    encrypted = encrypt_payload(payload.model_dump(mode="json"))
    complete_count = sum(record.review.complete for record in records)
    issue_count = sum(
        len(record.review.candidate_issue_codes)
        for record in records
        if record.review.complete
    )
    errors = Counter(
        _safe_code(record.review.error_code)
        for record in records
        if not record.review.complete
    )
    reference_hashes, judge_hash = transformed_prompt_identities_v7()
    target = review_target_count
    return stamp_v4(TransformedReviewArtifactV7(
        experiment_id=experiment_id,
        generated_at=generated_at,
        updated_at=_utcnow(),
        status="complete" if len(records) == target else "in_progress",
        source_holdout_hash=holdout.content_hash,
        source_selection_hash=selection.content_hash,
        source_review_hash=source_reviews.content_hash,
        source_model_identity_hash=preregistration.model_identity_hash,
        transformed_model_identity_hash=transformed_model_identity_hash,
        transformed_reference_prompt_identity_hashes=reference_hashes,
        transformed_judge_prompt_identity_hash=judge_hash,
        transform_implementation_hash=transform_implementation_hash_v7(),
        review_scope=review_scope,
        source_selection_case_count=selection.selected_case_count,
        review_target_count=target,
        review_count=len(records),
        complete_review_count=complete_count,
        review_coverage=complete_count / target if target else 0.0,
        candidate_issue_count=issue_count,
        incomplete_error_code_counts=dict(sorted(errors.items())),
        encrypted_checkpoint=EncryptedBlob(**encrypted),
    ))


def decrypt_transformed_reviews_v7(
    artifact: TransformedReviewArtifactV7,
) -> list[TransformedReviewRecordV7]:
    """Decrypt and validate transformed review records in memory."""

    if not verify_v4(artifact):
        raise ValueError("transformed review artifact hash is invalid")
    blob = artifact.encrypted_checkpoint
    payload = decrypt_payload(blob.ciphertext, blob.nonce, blob.key_version)
    if not isinstance(payload, dict) or _payload_hash(payload) != blob.payload_hash:
        raise ValueError("transformed review encrypted checkpoint hash mismatch")
    checkpoint = TransformedReviewCheckpointPayloadV7.model_validate(payload)
    expected = (
        artifact.experiment_id,
        artifact.source_selection_hash,
        artifact.transformed_model_identity_hash,
        artifact.review_scope,
        artifact.review_target_count,
    )
    actual = (
        checkpoint.experiment_id,
        checkpoint.source_selection_hash,
        checkpoint.transformed_model_identity_hash,
        checkpoint.review_scope,
        checkpoint.review_target_count,
    )
    if actual != expected:
        raise ValueError("transformed review encrypted checkpoint lineage mismatch")
    if len(checkpoint.records) != artifact.review_count:
        raise ValueError("transformed review encrypted checkpoint count mismatch")
    complete_count = sum(record.review.complete for record in checkpoint.records)
    issue_count = sum(
        len(record.review.candidate_issue_codes)
        for record in checkpoint.records
        if record.review.complete
    )
    errors = Counter(
        _safe_code(record.review.error_code)
        for record in checkpoint.records
        if not record.review.complete
    )
    if complete_count != artifact.complete_review_count:
        raise ValueError("transformed review complete count mismatch")
    if issue_count != artifact.candidate_issue_count:
        raise ValueError("transformed review candidate issue count mismatch")
    if dict(sorted(errors.items())) != artifact.incomplete_error_code_counts:
        raise ValueError("transformed review error counts mismatch")
    return list(checkpoint.records)


def _validate_source_lineage(
    *,
    holdout: HoldoutCollection,
    selection: SelectionManifest,
    source_reviews: AutomatedReviewBatch,
    preregistration: Preregistration,
) -> None:
    if not all(verify_v4(item) for item in (holdout, selection, source_reviews, preregistration)):
        raise ValueError("transformed review source artifact hash is invalid")
    if selection.holdout_hash != holdout.content_hash:
        raise ValueError("transformed review selection/holdout lineage mismatch")
    if source_reviews.selection_hash != selection.content_hash:
        raise ValueError("transformed review source-review lineage mismatch")
    if holdout.preregistration_hash != preregistration.content_hash:
        raise ValueError("transformed review preregistration lineage mismatch")
    if not (
        holdout.experiment_id
        == selection.experiment_id
        == source_reviews.experiment_id
        == preregistration.experiment_id
    ):
        raise ValueError("transformed review experiment lineage mismatch")
    if source_reviews.review_count != selection.selected_case_count:
        raise ValueError("transformed review source-review count mismatch")


def _resume_records(
    *,
    output_path: Path,
    holdout: HoldoutCollection,
    selection: SelectionManifest,
    source_reviews: AutomatedReviewBatch,
    preregistration: Preregistration,
    transformed_model_identity_hash: str,
    work: list[tuple[str, str]],
    review_scope: Literal[
        "all_selected_v4_cases", "policy_v7_certificate_selected_cases"
    ],
) -> tuple[datetime, list[TransformedReviewRecordV7]]:
    if not output_path.exists():
        return _utcnow(), []
    artifact = read_model(output_path, TransformedReviewArtifactV7)
    expected = (
        selection.experiment_id,
        holdout.content_hash,
        selection.content_hash,
        source_reviews.content_hash,
        preregistration.model_identity_hash,
        transformed_model_identity_hash,
        transform_implementation_hash_v7(),
        review_scope,
        len(work),
    )
    actual = (
        artifact.experiment_id,
        artifact.source_holdout_hash,
        artifact.source_selection_hash,
        artifact.source_review_hash,
        artifact.source_model_identity_hash,
        artifact.transformed_model_identity_hash,
        artifact.transform_implementation_hash,
        artifact.review_scope,
        artifact.review_target_count,
    )
    if actual != expected:
        raise ValueError("transformed review checkpoint lineage mismatch")
    records = decrypt_transformed_reviews_v7(artifact)
    if len(records) > len(work):
        raise ValueError("transformed review checkpoint exceeds worklist")
    for index, record in enumerate(records):
        if (
            record.source_question_hash != work[index][0]
            or record.source_case_payload_hash != work[index][1]
            or record.review.model_identity_hash != transformed_model_identity_hash
        ):
            raise ValueError("transformed review checkpoint is not a sealed-order prefix")
    return artifact.generated_at, records

__all__ = [
    "TRANSFORMED_REVIEW_PROTOCOL_V7",
    "ReviewerV7",
    "TransformedReviewArtifactV7",
    "TransformedReviewCheckpointPayloadV7",
    "TransformedReviewRecordV7",
    "_artifact",
    "_decrypt_source_case",
    "_resume_records",
    "_transform_case_v7",
    "_utcnow",
    "_validate_source_lineage",
    "decrypt_transformed_reviews_v7",
    "transform_implementation_hash_v7",
    "transformed_candidate_hash_v7",
    "transformed_case_payload_hash_v7",
    "transformed_prompt_identities_v7",
    "transformed_review_identity_v7",
]
