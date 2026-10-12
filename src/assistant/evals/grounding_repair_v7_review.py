"""Privacy-safe transformed-candidate review orchestration for Policy-v7."""

from __future__ import annotations

import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any, Callable, Literal

from src.assistant.evals.artifacts import read_model, write_model
from src.assistant.evals.grounding_repair_v4_protocol import (
    AutomatedReviewBatch,
    HoldoutCase,
    HoldoutCollection,
    Preregistration,
    SelectionManifest,
)
from src.assistant.evals.grounding_repair_v4_review import review_case_v4
from src.assistant.evals.grounding_repair_v4_review_transport import (
    TransportExhaustedError,
)
from src.assistant.evals.grounding_repair_v7_review_support import (
    TRANSFORMED_REVIEW_PROTOCOL_V7,
    ReviewerV7,
    TransformedReviewArtifactV7,
    TransformedReviewCheckpointPayloadV7,
    TransformedReviewRecordV7,
    _artifact,
    _decrypt_source_case,
    _resume_records,
    _transform_case_v7,
    _utcnow,
    _validate_source_lineage,
    decrypt_transformed_reviews_v7,
    transform_implementation_hash_v7,
    transformed_candidate_hash_v7,
    transformed_case_payload_hash_v7,
    transformed_prompt_identities_v7,
    transformed_review_identity_v7,
)

def _review_group_v7(
    *,
    group: list[tuple[str, str]],
    cases_by_hash: dict[str, HoldoutCase],
    transformed_model_identity_hash: str,
    attempts: int,
    reviewer: ReviewerV7,
    call: Any | None,
) -> list[TransformedReviewRecordV7]:
    prepared: list[
        tuple[str, str, str, str, dict[str, Any]]
    ] = []
    for question_hash, source_payload_hash in group:
        case = cases_by_hash.get(question_hash)
        if case is None:
            raise ValueError("transformed review selected case is missing")
        payload = _decrypt_source_case(case, source_payload_hash)
        transformed, transformed_payload_hash, candidate_hash = _transform_case_v7(payload)
        prepared.append((
            question_hash,
            source_payload_hash,
            transformed_payload_hash,
            candidate_hash,
            transformed,
        ))

    resolved: list[TransformedReviewRecordV7 | None] = [None] * len(prepared)
    transport_failure: TransportExhaustedError | None = None
    with ThreadPoolExecutor(
        max_workers=len(prepared), thread_name_prefix="policy-v7-transformed-review"
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
                question_hash,
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
        for (
            index,
            question_hash,
            source_payload_hash,
            transformed_payload_hash,
            candidate_hash,
            future,
        ) in futures:
            try:
                review = future.result()
                resolved[index] = TransformedReviewRecordV7(
                    source_question_hash=question_hash,
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


def review_selection_resumable_v7(
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
    reviewer: ReviewerV7 = review_case_v4,
    work: list[tuple[str, str]] | None = None,
    review_scope: Literal[
        "all_selected_v4_cases", "policy_v7_certificate_selected_cases"
    ] = "all_selected_v4_cases",
) -> TransformedReviewArtifactV7:
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
    full_positions = {item: index for index, item in enumerate(full_work)}
    if any(item not in full_positions for item in active_work):
        raise ValueError("transformed review worklist is outside the sealed selection")
    active_positions = [full_positions[item] for item in active_work]
    if active_positions != sorted(active_positions):
        raise ValueError("transformed review worklist is not in sealed order")
    if review_scope == "all_selected_v4_cases" and active_work != full_work:
        raise ValueError("all-case transformed review scope requires the complete selection")
    transformed_identity = transformed_review_identity_v7(preregistration)
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
        return read_model(output_path, TransformedReviewArtifactV7)

    if preflight is not None:
        preflight()
    cases_by_hash = {item.question_hash: item for item in holdout.cases}
    worker_count = max(1, min(int(concurrency), 8, len(active_work) or 1))
    def checkpoint() -> TransformedReviewArtifactV7:
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
                resolved = _review_group_v7(
                    group=group,
                    cases_by_hash=cases_by_hash,
                    transformed_model_identity_hash=transformed_identity,
                    attempts=attempts,
                    reviewer=reviewer,
                    call=call,
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


def review_selection_from_paths_v7(
    *,
    artifact_dir: Path,
    output_path: Path,
    attempts: int = 3,
    concurrency: int = 1,
    resume: bool = True,
    call: Any | None = None,
    preflight: Callable[[], Any] | None = None,
) -> TransformedReviewArtifactV7:
    root = Path(artifact_dir)
    return review_selection_resumable_v7(
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
    "TRANSFORMED_REVIEW_PROTOCOL_V7",
    "TransformedReviewArtifactV7",
    "TransformedReviewCheckpointPayloadV7",
    "TransformedReviewRecordV7",
    "decrypt_transformed_reviews_v7",
    "review_selection_from_paths_v7",
    "review_selection_resumable_v7",
    "transform_implementation_hash_v7",
    "transformed_candidate_hash_v7",
    "transformed_case_payload_hash_v7",
    "transformed_prompt_identities_v7",
    "transformed_review_identity_v7",
]
