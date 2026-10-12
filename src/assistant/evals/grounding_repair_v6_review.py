"""Privacy-safe transformed-candidate reviews for offline Policy-v6 research.

The evaluator reuses the independent Reference-R and blinded A/B judge from the
sealed Policy-v4 workflow, but replaces only the candidate answer in memory with
the deterministic Policy-v6 filtered answer. Checkpoints encrypt every per-case
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
from src.assistant.evals.grounding_repair_candidate_v6 import (
    POLICY_VERSION_V6,
    filter_grounding_repair_candidate_v6,
)
from src.assistant.evals.grounding_repair_certificate_v5_projection import (
    ProjectionCacheV5,
)
from src.assistant.evals.grounding_repair_certificate_v5_rules import (
    LineContractCacheV5,
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

TRANSFORMED_REVIEW_PROTOCOL_V6 = "policy-v6-transformed-reference-r-blinded-review-v1"
_SAFE_CODE_RE = re.compile(r"^[A-Za-z0-9_.:+-]+$")
ReviewerV6 = Callable[..., AutomatedCaseReview]


class TransformedReviewRecordV6(V4Model):
    """Encrypted-only record linking one source case to its transformed review."""

    source_question_hash: str = Field(min_length=8)
    source_case_payload_hash: str = Field(min_length=8)
    transformed_case_payload_hash: str = Field(min_length=8)
    transformed_candidate_hash: str = Field(min_length=8)
    review: AutomatedCaseReview

    @model_validator(mode="after")
    def validate_review_lineage(self) -> "TransformedReviewRecordV6":
        if self.review.question_hash != self.source_question_hash:
            raise ValueError("transformed review question hash mismatch")
        if self.review.case_payload_hash != self.transformed_case_payload_hash:
            raise ValueError("transformed review payload hash mismatch")
        return self


class TransformedReviewCheckpointPayloadV6(V4Model):
    """Sensitive checkpoint payload that is encrypted before persistence."""

    schema_version: Literal[1] = 1
    experiment_id: str = Field(min_length=3)
    protocol: Literal[TRANSFORMED_REVIEW_PROTOCOL_V6] = TRANSFORMED_REVIEW_PROTOCOL_V6
    source_selection_hash: str = Field(min_length=8)
    transformed_model_identity_hash: str = Field(min_length=8)
    review_scope: Literal[
        "all_selected_v4_cases", "policy_v6_certificate_selected_cases"
    ]
    review_target_count: int = Field(ge=0)
    records: list[TransformedReviewRecordV6]


class TransformedReviewArtifactV6(V4Model):
    """Aggregate-only plaintext envelope for encrypted transformed reviews."""

    schema_version: Literal[1] = 1
    experiment_id: str = Field(min_length=3)
    generated_at: datetime
    updated_at: datetime
    status: Literal["in_progress", "complete"]
    policy_version: Literal[POLICY_VERSION_V6] = POLICY_VERSION_V6
    review_protocol: Literal[TRANSFORMED_REVIEW_PROTOCOL_V6] = TRANSFORMED_REVIEW_PROTOCOL_V6
    source_holdout_hash: str = Field(min_length=8)
    source_selection_hash: str = Field(min_length=8)
    source_review_hash: str = Field(min_length=8)
    source_model_identity_hash: str = Field(min_length=8)
    transformed_model_identity_hash: str = Field(min_length=8)
    transformed_reference_prompt_identity_hashes: list[str] = Field(min_length=1)
    transformed_judge_prompt_identity_hash: str = Field(min_length=8)
    transform_implementation_hash: str = Field(min_length=8)
    review_scope: Literal[
        "all_selected_v4_cases", "policy_v6_certificate_selected_cases"
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
    def validate_aggregate_counts(self) -> "TransformedReviewArtifactV6":
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


def transformed_review_identity_v6(preregistration: Preregistration) -> str:
    """Bind the transformed evaluator identity to source routing and v6 protocol."""

    return hash_identity({
        "protocol": TRANSFORMED_REVIEW_PROTOCOL_V6,
        "policy_version": POLICY_VERSION_V6,
        "source_model_identity_hash": preregistration.model_identity_hash,
        "transform_implementation_hash": transform_implementation_hash_v6(),
        "prompt_identity": transformed_prompt_identities_v6(),
    })


def transformed_prompt_identities_v6() -> tuple[list[str], str]:
    """Return distinct lineage identities for the reused independent evaluator."""

    reference_hashes, judge_hash = prompt_hashes_v4()
    transformed_reference = [
        hash_identity({
            "protocol": TRANSFORMED_REVIEW_PROTOCOL_V6,
            "role": "reference_r",
            "source_prompt_hash": value,
        })
        for value in reference_hashes
    ]
    transformed_judge = hash_identity({
        "protocol": TRANSFORMED_REVIEW_PROTOCOL_V6,
        "role": "blinded_ab_judge",
        "source_prompt_hash": judge_hash,
        "candidate_semantics": "policy_v6_deterministic_filtered_candidate",
    })
    return transformed_reference, transformed_judge


def transform_implementation_hash_v6() -> str:
    """Hash the offline transformation implementation used by the review."""

    paths = (
        Path(__file__).with_name("grounding_repair_candidate_v6.py"),
        Path(__file__).with_name("grounding_repair_certificate_v6.py"),
        Path(__file__).with_name("grounding_repair_certificate_v5_projection.py"),
        Path(__file__).with_name("grounding_repair_certificate_v5_rules.py"),
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



def transformed_candidate_hash_v6(candidate_answer: str) -> str:
    """Return the identity used to correlate one in-memory transformed answer."""

    return hash_identity({
        "policy_version": POLICY_VERSION_V6,
        "candidate_answer": str(candidate_answer or ""),
    })


def transformed_case_payload_hash_v6(
    *, source_case_payload_hash: str, transformed_candidate_hash: str,
) -> str:
    """Return a derived review identity without persisting transformed plaintext."""

    return hash_identity({
        "protocol": TRANSFORMED_REVIEW_PROTOCOL_V6,
        "source_case_payload_hash": source_case_payload_hash,
        "transformed_candidate_hash": transformed_candidate_hash,
    })

def _transform_case_v6(
    payload: dict[str, Any],
    *,
    contract_cache: LineContractCacheV5,
) -> tuple[dict[str, Any], str, str]:
    sources = [item for item in payload.get("sources") or [] if isinstance(item, dict)]
    tool_results = [
        item for item in payload.get("tool_results") or [] if isinstance(item, dict)
    ]
    projection_cache = ProjectionCacheV5.build(sources, tool_results)
    filtered = filter_grounding_repair_candidate_v6(
        candidate_answer=str(payload.get("candidate_answer") or ""),
        question=str(payload.get("question") or ""),
        sources=sources,
        tool_results=tool_results,
        projection_cache=projection_cache,
        contract_cache=contract_cache,
    )
    transformed = dict(payload)
    transformed["candidate_answer"] = filtered.candidate_answer
    candidate_hash = transformed_candidate_hash_v6(filtered.candidate_answer)
    transformed_payload_hash = transformed_case_payload_hash_v6(
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
        "all_selected_v4_cases", "policy_v6_certificate_selected_cases"
    ],
    review_target_count: int,
    records: list[TransformedReviewRecordV6],
) -> TransformedReviewCheckpointPayloadV6:
    return TransformedReviewCheckpointPayloadV6(
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
    records: list[TransformedReviewRecordV6],
    review_scope: Literal[
        "all_selected_v4_cases", "policy_v6_certificate_selected_cases"
    ],
    review_target_count: int,
) -> TransformedReviewArtifactV6:
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
    reference_hashes, judge_hash = transformed_prompt_identities_v6()
    target = review_target_count
    return stamp_v4(TransformedReviewArtifactV6(
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
        transform_implementation_hash=transform_implementation_hash_v6(),
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


def decrypt_transformed_reviews_v6(
    artifact: TransformedReviewArtifactV6,
) -> list[TransformedReviewRecordV6]:
    """Decrypt and validate transformed review records in memory."""

    if not verify_v4(artifact):
        raise ValueError("transformed review artifact hash is invalid")
    blob = artifact.encrypted_checkpoint
    payload = decrypt_payload(blob.ciphertext, blob.nonce, blob.key_version)
    if not isinstance(payload, dict) or _payload_hash(payload) != blob.payload_hash:
        raise ValueError("transformed review encrypted checkpoint hash mismatch")
    checkpoint = TransformedReviewCheckpointPayloadV6.model_validate(payload)
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
        "all_selected_v4_cases", "policy_v6_certificate_selected_cases"
    ],
) -> tuple[datetime, list[TransformedReviewRecordV6]]:
    if not output_path.exists():
        return _utcnow(), []
    artifact = read_model(output_path, TransformedReviewArtifactV6)
    expected = (
        selection.experiment_id,
        holdout.content_hash,
        selection.content_hash,
        source_reviews.content_hash,
        preregistration.model_identity_hash,
        transformed_model_identity_hash,
        transform_implementation_hash_v6(),
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
    records = decrypt_transformed_reviews_v6(artifact)
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


def _review_group_v6(
    *,
    group: list[tuple[str, str]],
    cases_by_hash: dict[str, HoldoutCase],
    transformed_model_identity_hash: str,
    attempts: int,
    reviewer: ReviewerV6,
    call: Any | None,
    contract_cache: LineContractCacheV5,
) -> list[TransformedReviewRecordV6]:
    prepared: list[
        tuple[str, str, str, str, dict[str, Any]]
    ] = []
    for question_hash, source_payload_hash in group:
        case = cases_by_hash.get(question_hash)
        if case is None:
            raise ValueError("transformed review selected case is missing")
        payload = _decrypt_source_case(case, source_payload_hash)
        transformed, transformed_payload_hash, candidate_hash = _transform_case_v6(
            payload, contract_cache=contract_cache
        )
        prepared.append((
            question_hash,
            source_payload_hash,
            transformed_payload_hash,
            candidate_hash,
            transformed,
        ))

    resolved: list[TransformedReviewRecordV6 | None] = [None] * len(prepared)
    transport_failure: TransportExhaustedError | None = None
    with ThreadPoolExecutor(
        max_workers=len(prepared), thread_name_prefix="policy-v6-transformed-review"
    ) as pool:
        futures = []
        for index, (
            question_hash,
            source_payload_hash,
            transformed_payload_hash,
            candidate_hash,
            transformed,
        ) in enumerate(prepared):
            futures.append((
                index,
                source_payload_hash,
                transformed_payload_hash,
                candidate_hash,
                pool.submit(
                    reviewer,
                    question_hash=question_hash,
                    case_payload_hash=transformed_payload_hash,
                    case=transformed,
                    model_identity_hash=transformed_model_identity_hash,
                    attempts=attempts,
                    call=call,
                ),
            ))
        for index, source_payload_hash, transformed_payload_hash, candidate_hash, future in futures:
            try:
                review = future.result()
                resolved[index] = TransformedReviewRecordV6(
                    source_question_hash=review.question_hash,
                    source_case_payload_hash=source_payload_hash,
                    transformed_case_payload_hash=transformed_payload_hash,
                    transformed_candidate_hash=candidate_hash,
                    review=review,
                )
            except TransportExhaustedError as exc:
                transport_failure = transport_failure or exc
    if transport_failure is not None:
        raise transport_failure
    if any(item is None for item in resolved):
        raise RuntimeError("transformed review group did not resolve every case")
    return [item for item in resolved if item is not None]


def review_selection_resumable_v6(
    *,
    holdout: HoldoutCollection,
    selection: SelectionManifest,
    source_reviews: AutomatedReviewBatch,
    preregistration: Preregistration,
    output_path: Path,
    attempts: int = 3,
    concurrency: int = 1,
    resume: bool = True,
    call: Any | None = None,
    preflight: Callable[[], Any] | None = None,
    circuit_pause_seconds: float = 15.0,
    reviewer: ReviewerV6 = review_case_v4,
    work: list[tuple[str, str]] | None = None,
    review_scope: Literal[
        "all_selected_v4_cases", "policy_v6_certificate_selected_cases"
    ] = "all_selected_v4_cases",
) -> TransformedReviewArtifactV6:
    """Review all selected transformed candidates with encrypted resumable checkpoints."""

    _validate_source_lineage(
        holdout=holdout,
        selection=selection,
        source_reviews=source_reviews,
        preregistration=preregistration,
    )
    output_path = Path(output_path)
    if output_path.exists() and not resume:
        raise FileExistsError(output_path)
    full_work = list(zip(
        selection.selected_question_hashes,
        selection.selected_case_payload_hashes,
        strict=True,
    ))
    active_work = list(work) if work is not None else full_work
    if len(active_work) != len(set(active_work)):
        raise ValueError("transformed review worklist contains duplicate entries")
    if any(item not in set(full_work) for item in active_work):
        raise ValueError("transformed review worklist is outside the sealed selection")
    if review_scope == "all_selected_v4_cases" and active_work != full_work:
        raise ValueError("all-case transformed review scope requires the complete selection")
    transformed_identity = transformed_review_identity_v6(preregistration)
    generated_at, records = _resume_records(
        output_path=output_path,
        holdout=holdout,
        selection=selection,
        source_reviews=source_reviews,
        preregistration=preregistration,
        transformed_model_identity_hash=transformed_identity,
        work=active_work,
        review_scope=review_scope,
    ) if resume else (_utcnow(), [])
    if len(records) == len(active_work):
        return read_model(output_path, TransformedReviewArtifactV6)

    if preflight is not None:
        preflight()
    cases_by_hash = {item.question_hash: item for item in holdout.cases}
    worker_count = max(1, min(int(concurrency), 8, len(active_work) or 1))
    contract_cache = LineContractCacheV5()

    def checkpoint() -> TransformedReviewArtifactV6:
        artifact = _artifact(
            experiment_id=selection.experiment_id,
            generated_at=generated_at,
            holdout=holdout,
            selection=selection,
            source_reviews=source_reviews,
            preregistration=preregistration,
            transformed_model_identity_hash=transformed_identity,
            records=records,
            review_scope=review_scope,
            review_target_count=len(active_work),
        )
        write_model(output_path, artifact)
        return artifact

    for offset in range(len(records), len(active_work), worker_count):
        group = active_work[offset:offset + worker_count]
        for circuit_attempt in range(2):
            try:
                resolved = _review_group_v6(
                    group=group,
                    cases_by_hash=cases_by_hash,
                    transformed_model_identity_hash=transformed_identity,
                    attempts=attempts,
                    reviewer=reviewer,
                    call=call,
                    contract_cache=contract_cache,
                )
                break
            except TransportExhaustedError:
                if circuit_attempt > 0 or preflight is None:
                    raise
                time.sleep(max(0.0, float(circuit_pause_seconds)))
                preflight()
        else:
            raise AssertionError("unreachable transformed-review circuit state")
        records.extend(resolved)
        checkpoint()
    return checkpoint()


def review_selection_from_paths_v6(
    *,
    artifact_dir: Path,
    output_path: Path,
    attempts: int = 3,
    concurrency: int = 1,
    resume: bool = True,
    call: Any | None = None,
    preflight: Callable[[], Any] | None = None,
) -> TransformedReviewArtifactV6:
    root = Path(artifact_dir)
    return review_selection_resumable_v6(
        holdout=read_model(root / "holdout.json", HoldoutCollection),
        selection=read_model(root / "selection.json", SelectionManifest),
        source_reviews=read_model(root / "reviews.json", AutomatedReviewBatch),
        preregistration=read_model(root / "preregistration.json", Preregistration),
        output_path=output_path,
        attempts=attempts,
        concurrency=concurrency,
        resume=resume,
        call=call,
        preflight=preflight,
    )


__all__ = [
    "TRANSFORMED_REVIEW_PROTOCOL_V6",
    "TransformedReviewArtifactV6",
    "TransformedReviewCheckpointPayloadV6",
    "TransformedReviewRecordV6",
    "decrypt_transformed_reviews_v6",
    "review_selection_from_paths_v6",
    "review_selection_resumable_v6",
    "transform_implementation_hash_v6",
    "transformed_candidate_hash_v6",
    "transformed_case_payload_hash_v6",
    "transformed_prompt_identities_v6",
    "transformed_review_identity_v6",
]
