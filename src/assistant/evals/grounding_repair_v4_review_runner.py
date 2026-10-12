"""Resumable, bounded-concurrency execution for Policy-v4 automated reviews."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path
import time
from typing import Any, Callable

from src.assistant.evals.artifacts import read_model, write_model
from src.assistant.evals.grounding_repair_v4_protocol import (
    AutomatedCaseReview,
    AutomatedReviewBatch,
    HoldoutCase,
    SelectionManifest,
    stamp_v4,
    verify_v4,
)
from src.assistant.evals.grounding_repair_v4_review import review_case_v4
from src.assistant.evals.grounding_repair_v4_review_transport import (
    TransportExhaustedError,
    TransportRetryState,
    call_stage,
)
from src.llm.client import StructuredLLMResponse, llm_call_structured
from src.llm.routing import get_backup_config, get_model_for_task
from src.observability.payload_crypto import decrypt_payload

_PREFLIGHT_SCHEMA = {
    "type": "object",
    "properties": {"ok": {"type": "boolean"}},
    "required": ["ok"],
}


def _model_name(value: str) -> str:
    cleaned = str(value or "").strip()
    if cleaned.startswith("backup:"):
        cleaned = cleaned.removeprefix("backup:")
    return cleaned.rsplit("/", 1)[-1]


def preflight_review_v4(*, active_model: str, backup_model: str, timeout_sec: int = 60) -> str:
    """Verify evaluator routing and one synthetic structured response before decrypting cases."""
    expected_active = _model_name(active_model)
    expected_backup = _model_name(backup_model)
    routed_active = _model_name(get_model_for_task("reasoning").primary)
    routed_backup = _model_name(get_backup_config().primary)
    if routed_active != expected_active:
        raise RuntimeError("Policy-v4 active evaluator route differs from preregistration")
    if routed_backup != expected_backup:
        raise RuntimeError("Policy-v4 backup evaluator route differs from preregistration")
    def structured_preflight(
        _prompt: str, _payload: dict[str, Any], _schema: dict[str, Any],
    ) -> tuple[dict[str, Any], str]:
        response = llm_call_structured(
            "reasoning",
            [{"role": "user", "content": "Return strict JSON with ok=true."}],
            _PREFLIGHT_SCHEMA,
            temperature=0.0,
            timeout_sec=timeout_sec,
            return_metadata=True,
        )
        if not isinstance(response, StructuredLLMResponse) or not isinstance(response.data, dict):
            return {}, ""
        return response.data, response.model

    data, model = call_stage(
        structured_preflight,
        TransportRetryState(),
        "preflight",
        "Return strict JSON with ok=true.",
        {},
        _PREFLIGHT_SCHEMA,
    )
    if data.get("ok") is not True:
        raise RuntimeError("Policy-v4 evaluator preflight returned an invalid response")
    execution_model = _model_name(model)
    if execution_model not in {expected_active, expected_backup}:
        raise RuntimeError("Policy-v4 evaluator executed with an unregistered model")
    return execution_model


def _checkpoint(
    *, output_path: Path, experiment_id: str, selection_hash: str,
    generated_at: datetime, reviews: list[AutomatedCaseReview],
) -> AutomatedReviewBatch:
    batch = stamp_v4(AutomatedReviewBatch(
        experiment_id=experiment_id,
        generated_at=generated_at,
        selection_hash=selection_hash,
        review_count=len(reviews),
        reviews=reviews,
    ))
    write_model(output_path, batch)
    return batch


def _resume_prefix(
    *, output_path: Path, experiment_id: str, selection: SelectionManifest,
    model_identity_hash: str,
) -> tuple[datetime, list[AutomatedCaseReview]]:
    if not output_path.exists():
        return datetime.now(timezone.utc), []
    existing = read_model(output_path, AutomatedReviewBatch)
    if not verify_v4(existing):
        raise ValueError("Policy-v4 review checkpoint hash is invalid")
    if existing.experiment_id != experiment_id or existing.selection_hash != selection.content_hash:
        raise ValueError("Policy-v4 review checkpoint lineage mismatch")
    if existing.review_count != len(existing.reviews):
        raise ValueError("Policy-v4 review checkpoint count mismatch")
    if existing.review_count > selection.selected_case_count:
        raise ValueError("Policy-v4 review checkpoint exceeds sealed selection")
    for index, item in enumerate(existing.reviews):
        if (
            item.question_hash != selection.selected_question_hashes[index]
            or item.case_payload_hash != selection.selected_case_payload_hashes[index]
            or item.model_identity_hash != model_identity_hash
        ):
            raise ValueError("Policy-v4 review checkpoint is not a valid sealed-order prefix")
    return existing.generated_at, list(existing.reviews)


def _missing_case_review(
    *, question_hash: str, case_payload_hash: str, model_identity_hash: str,
) -> AutomatedCaseReview:
    return AutomatedCaseReview(
        question_hash=question_hash,
        case_payload_hash=case_payload_hash,
        complete=False,
        preferred_answer="incomplete",
        model_identity_hash=model_identity_hash,
        error_code="selected_case_payload_unavailable",
    )


def _review_group(
    *, group: list[tuple[str, str]], by_hash: dict[str, HoldoutCase],
    model_identity_hash: str, attempts: int, call: Any | None,
) -> list[AutomatedCaseReview]:
    resolved: list[AutomatedCaseReview | None] = [None] * len(group)
    transport_failure: TransportExhaustedError | None = None
    with ThreadPoolExecutor(max_workers=len(group), thread_name_prefix="policy-v4-review") as pool:
        futures: list[tuple[int, Any]] = []
        for index, (question_hash, payload_hash) in enumerate(group):
            item = by_hash.get(question_hash)
            if item is None or item.encrypted_case_payload is None:
                resolved[index] = _missing_case_review(
                    question_hash=question_hash,
                    case_payload_hash=payload_hash,
                    model_identity_hash=model_identity_hash,
                )
                continue
            blob = item.encrypted_case_payload
            case = decrypt_payload(blob.ciphertext, blob.nonce, blob.key_version)
            futures.append((index, pool.submit(
                review_case_v4,
                question_hash=question_hash,
                case_payload_hash=payload_hash,
                case=case,
                model_identity_hash=model_identity_hash,
                attempts=attempts,
                call=call,
            )))
        for index, future in futures:
            try:
                resolved[index] = future.result()
            except TransportExhaustedError as exc:
                transport_failure = transport_failure or exc
    if transport_failure is not None:
        raise transport_failure
    if any(item is None for item in resolved):
        raise RuntimeError("Policy-v4 review batch did not resolve every sealed case")
    return [item for item in resolved if item is not None]


def review_worklist_resumable_v4(
    *, work: list[tuple[str, str]], selected_cases: list[HoldoutCase],
    model_identity_hash: str, existing_reviews: list[AutomatedCaseReview] | None = None,
    attempts: int = 3, concurrency: int = 4, call: Any | None = None,
    preflight: Callable[[], Any] | None = None, circuit_pause_seconds: float = 15.0,
    checkpoint: Callable[[list[AutomatedCaseReview]], Any] | None = None,
) -> list[AutomatedCaseReview]:
    """Review an ordered metadata-only worklist with batch rollback and recovery."""
    if len(work) != len(set(work)):
        raise ValueError("Policy-v4 review worklist contains duplicate entries")
    reviews = list(existing_reviews or [])
    if len(reviews) > len(work):
        raise ValueError("Policy-v4 review prefix exceeds worklist")
    for index, item in enumerate(reviews):
        if (item.question_hash, item.case_payload_hash) != work[index]:
            raise ValueError("Policy-v4 review prefix is not aligned with the worklist")
        if item.model_identity_hash != model_identity_hash:
            raise ValueError("Policy-v4 review prefix model identity mismatch")
    by_hash = {item.question_hash: item for item in selected_cases}
    worker_count = max(1, min(int(concurrency), 8, len(work) or 1))
    for offset in range(len(reviews), len(work), worker_count):
        group = work[offset:offset + worker_count]
        for circuit_attempt in range(2):
            try:
                resolved = _review_group(
                    group=group,
                    by_hash=by_hash,
                    model_identity_hash=model_identity_hash,
                    attempts=attempts,
                    call=call,
                )
                break
            except TransportExhaustedError:
                if circuit_attempt > 0 or preflight is None:
                    raise
                time.sleep(max(0.0, float(circuit_pause_seconds)))
                preflight()
        else:
            raise AssertionError("unreachable Policy-v4 circuit state")
        reviews.extend(resolved)
        if checkpoint is not None:
            checkpoint(list(reviews))
    return reviews


def review_selection_resumable_v4(
    *, experiment_id: str, selection: SelectionManifest, selected_cases: list[HoldoutCase],
    model_identity_hash: str, output_path: Path, attempts: int = 3,
    concurrency: int = 4, resume: bool = True, call: Any | None = None,
    preflight: Callable[[], Any] | None = None, circuit_pause_seconds: float = 15.0,
) -> AutomatedReviewBatch:
    """Review sealed batches, retrying transport-failed batches before checkpointing."""
    if not verify_v4(selection):
        raise ValueError("selection manifest hash is invalid")
    output_path = Path(output_path)
    if output_path.exists() and not resume:
        raise FileExistsError(output_path)
    generated_at, reviews = _resume_prefix(
        output_path=output_path,
        experiment_id=experiment_id,
        selection=selection,
        model_identity_hash=model_identity_hash,
    ) if resume else (datetime.now(timezone.utc), [])
    work = list(zip(
        selection.selected_question_hashes,
        selection.selected_case_payload_hashes,
        strict=True,
    ))

    def checkpoint(resolved: list[AutomatedCaseReview]) -> AutomatedReviewBatch:
        return _checkpoint(
            output_path=output_path,
            experiment_id=experiment_id,
            selection_hash=selection.content_hash,
            generated_at=generated_at,
            reviews=resolved,
        )

    reviews = review_worklist_resumable_v4(
        work=work,
        selected_cases=selected_cases,
        model_identity_hash=model_identity_hash,
        existing_reviews=reviews,
        attempts=attempts,
        concurrency=concurrency,
        call=call,
        preflight=preflight,
        circuit_pause_seconds=circuit_pause_seconds,
        checkpoint=checkpoint,
    )
    if len(reviews) == len(work):
        return checkpoint(reviews)
    raise RuntimeError("Policy-v4 review checkpoint did not reach the sealed selection boundary")


__all__ = [
    "preflight_review_v4",
    "review_selection_resumable_v4",
    "review_worklist_resumable_v4",
]
