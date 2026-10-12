"""Offline Policy-v4 grounding-repair research-screen experiment.

Sensitive questions, answers, evidence, and replay details are decrypted only in memory.
The durable report contains aggregate metrics, gate results, hashes, and no per-case rows.
"""

from __future__ import annotations

import hashlib
import json
import math
from contextvars import ContextVar
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from src.assistant import grounding
from src.assistant.evals.artifacts import write_model
from src.assistant.evals.grounding_repair_certificate_v4 import (
    evaluate_grounding_repair_certificate_v4,
)
from src.assistant.evals.grounding_repair_v4_replay import (
    ReplayCache,
    prime_replay_cache,
    replay_case_v4,
)
from src.assistant.evals.grounding_repair_v4_protocol import (
    AutomatedCaseReview,
    AutomatedReviewBatch,
    ExperimentReport,
    HistoricalExclusionManifest,
    HoldoutCase,
    HoldoutCollection,
    Preregistration,
    QuestionManifest,
    ResearchScreenStatus,
    SelectionManifest,
    stamp_v4,
    verify_v4,
)
from src.observability.payload_crypto import decrypt_payload


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _p95(values: list[float]) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    return ordered[max(0, math.ceil(0.95 * len(ordered)) - 1)]


def _payload_hash(value: Any) -> str:
    plaintext = json.dumps(value, ensure_ascii=False, default=str).encode("utf-8")
    return hashlib.sha256(plaintext).hexdigest()


def _oracle_safe(review: AutomatedCaseReview, prereg: Preregistration) -> bool:
    if not review.complete or review.candidate_score is None or review.baseline_score is None:
        return False
    return (
        review.candidate_score >= prereg.gates.candidate_minimum_score
        and not review.candidate_issue_codes
        and review.candidate_score
        >= review.baseline_score - prereg.gates.candidate_noninferiority_margin
    )


def _lineage_valid(
    *,
    historical: HistoricalExclusionManifest,
    manifest: QuestionManifest,
    preregistration: Preregistration,
    holdout: HoldoutCollection,
    selection: SelectionManifest,
    reviews: AutomatedReviewBatch,
) -> bool:
    return (
        manifest.historical_exclusion_hash == historical.content_hash
        and preregistration.question_manifest_hash == manifest.content_hash
        and preregistration.historical_exclusion_hash == historical.content_hash
        and holdout.question_manifest_hash == manifest.content_hash
        and holdout.preregistration_hash == preregistration.content_hash
        and selection.holdout_hash == holdout.content_hash
        and reviews.selection_hash == selection.content_hash
        and preregistration.experiment_id == holdout.experiment_id
        == selection.experiment_id == reviews.experiment_id
    )


def _case_payload(case: HoldoutCase) -> dict[str, Any] | None:
    blob = case.encrypted_case_payload
    if blob is None or not case.case_payload_hash:
        return None
    try:
        payload = decrypt_payload(blob.ciphertext, blob.nonce, blob.key_version)
    except Exception:
        return None
    if not isinstance(payload, dict):
        return None
    if _payload_hash(payload) != blob.payload_hash or case.case_payload_hash != blob.payload_hash:
        return None
    return payload


_ACTIVE_REPLAY_CACHE: ContextVar[ReplayCache | None] = ContextVar(
    "grounding_repair_v4_replay_cache", default=None
)


def _replay_case(case: HoldoutCase, payload: dict[str, Any]) -> dict[str, Any]:
    del case
    cache = _ACTIVE_REPLAY_CACHE.get() or ReplayCache()
    return replay_case_v4(
        payload,
        cache,
        certificate_evaluator=evaluate_grounding_repair_certificate_v4,
    )


def run_experiment_v4(
    *,
    historical: HistoricalExclusionManifest,
    manifest: QuestionManifest,
    preregistration: Preregistration,
    holdout: HoldoutCollection,
    selection: SelectionManifest,
    reviews: AutomatedReviewBatch,
    output_path: Path | None = None,
    generated_at: datetime | None = None,
) -> ExperimentReport:
    """Replay the selected holdout and produce an aggregate-only research report."""
    artifacts_valid = all(
        verify_v4(item)
        for item in (historical, manifest, preregistration, holdout, selection, reviews)
    )
    lineage_valid = _lineage_valid(
        historical=historical,
        manifest=manifest,
        preregistration=preregistration,
        holdout=holdout,
        selection=selection,
        reviews=reviews,
    )
    cases = {item.question_hash: item for item in holdout.cases}
    review_by_hash = {item.question_hash: item for item in reviews.reviews}
    replay_count = candidate_parity = initial_parity = candidate_error_parity = 0
    selected_count = false_positives = selected_issue_count = 0
    certificate_times: list[float] = []
    full_times: list[float] = []
    selected_cases: list[HoldoutCase] = []
    complete_reviews = 0

    replay_inputs: list[tuple[HoldoutCase, dict[str, Any], AutomatedCaseReview]] = []
    for question_hash, payload_hash in zip(
        selection.selected_question_hashes,
        selection.selected_case_payload_hashes,
        strict=True,
    ):
        case = cases.get(question_hash)
        review = review_by_hash.get(question_hash)
        if case is not None:
            selected_cases.append(case)
        if (
            case is None
            or review is None
            or case.case_payload_hash != payload_hash
            or review.case_payload_hash != payload_hash
        ):
            continue
        if review.complete:
            complete_reviews += 1
        payload = _case_payload(case)
        if payload is None:
            continue
        if (
            payload.get("question_hash") != case.question_hash
            or payload.get("request_hash") != case.request_hash
        ):
            continue
        replay_inputs.append((case, payload, review))

    replay_cache = ReplayCache()
    prime_replay_cache([payload for _, payload, _ in replay_inputs], replay_cache)
    replay_cache_token = _ACTIVE_REPLAY_CACHE.set(replay_cache)
    try:
        for case, payload, review in replay_inputs:
            try:
                replay = _replay_case(case, payload)
            except Exception:
                continue
            replay_count += 1
            candidate_parity += int(replay["candidate_parity"])
            initial_parity += int(replay["initial_error_parity"])
            candidate_error_parity += int(replay["candidate_error_parity"])
            certificate_times.append(float(replay["certificate_ms"]))
            full_times.append(float(replay["full_replay_ms"]))
            if replay["would_skip_repair"]:
                selected_count += 1
                selected_issue_count += len(review.candidate_issue_codes)
                false_positives += int(not _oracle_safe(review, preregistration))
    finally:
        _ACTIVE_REPLAY_CACHE.reset(replay_cache_token)

    target = preregistration.gates.repair_case_target
    complete_repair_count = sum(
        int(item.complete and item.repair_triggered) for item in selected_cases
    )
    distinct_request_count = len({item.request_hash for item in selected_cases})
    review_coverage = complete_reviews / target if target else 0.0
    projected_reduction = selected_count / target if target else 0.0
    certificate_p95 = _p95(certificate_times)
    full_replay_p95 = _p95(full_times)
    replay_parity = (
        replay_count == target
        and candidate_parity == target
        and initial_parity == target
        and candidate_error_parity == target
    )

    gates = {
        "input_artifacts_valid": artifacts_valid,
        "lineage_valid": lineage_valid,
        "selection_complete": selection.status == "complete" and selection.selected_case_count == target,
        "complete_repair_case_target_met": complete_repair_count == target,
        "distinct_request_target_met": distinct_request_count == preregistration.gates.distinct_request_target,
        "automated_review_coverage_met": review_coverage >= preregistration.gates.automated_review_coverage,
        "replay_candidate_and_error_parity": replay_parity,
        "minimum_certificate_selected_cases_met": selected_count >= preregistration.gates.minimum_selected_cases,
        "minimum_projected_repair_reduction_met": projected_reduction >= preregistration.gates.minimum_projected_repair_reduction,
        "false_positive_limit_met": false_positives <= preregistration.gates.maximum_false_positives,
        "selected_candidate_issue_limit_met": selected_issue_count <= preregistration.gates.maximum_selected_candidate_issue_count,
        "certificate_p95_met": certificate_p95 < preregistration.gates.maximum_certificate_p95_ms,
        "full_replay_p95_met": full_replay_p95 < preregistration.gates.maximum_full_replay_p95_ms,
    }
    incomplete_names = (
        "input_artifacts_valid", "lineage_valid", "selection_complete",
        "complete_repair_case_target_met", "distinct_request_target_met",
        "automated_review_coverage_met", "replay_candidate_and_error_parity",
    )
    safety_names = ("false_positive_limit_met", "selected_candidate_issue_limit_met")
    if not all(gates[name] for name in incomplete_names):
        status = ResearchScreenStatus.INCOMPLETE
    elif not all(gates[name] for name in safety_names):
        status = ResearchScreenStatus.FAILED_SAFETY
    elif not all(gates.values()):
        status = ResearchScreenStatus.INCONCLUSIVE
    else:
        status = ResearchScreenStatus.PASSED

    metrics: dict[str, int | float | bool | None] = {
        "holdout_case_count": len(holdout.cases),
        "complete_repair_case_count": complete_repair_count,
        "selected_case_count": selection.selected_case_count,
        "distinct_request_hash_count": distinct_request_count,
        "automated_review_complete_count": complete_reviews,
        "automated_review_coverage": review_coverage,
        "replay_complete_count": replay_count,
        "candidate_answer_parity_count": candidate_parity,
        "initial_error_parity_count": initial_parity,
        "candidate_error_parity_count": candidate_error_parity,
        "certificate_selected_count": selected_count,
        "projected_repair_reduction": projected_reduction,
        "false_positive_count": false_positives,
        "selected_candidate_issue_count": selected_issue_count,
        "certificate_p95_ms": certificate_p95,
        "full_replay_p95_ms": full_replay_p95,
    }
    report = stamp_v4(ExperimentReport(
        experiment_id=preregistration.experiment_id,
        generated_at=generated_at or _utcnow(),
        status=status,
        preregistration_hash=preregistration.content_hash,
        question_manifest_hash=manifest.content_hash,
        historical_exclusion_hash=historical.content_hash,
        holdout_hash=holdout.content_hash,
        selection_hash=selection.content_hash,
        review_hash=reviews.content_hash,
        metrics=metrics,
        gates=gates,
    ))
    if output_path is not None:
        write_model(Path(output_path), report)
    return report


__all__ = ["run_experiment_v4"]