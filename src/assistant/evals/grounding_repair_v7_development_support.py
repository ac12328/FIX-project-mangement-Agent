"""In-memory helpers for offline Policy-v7 development replay."""

from __future__ import annotations

import hashlib
import json
import math
import re
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from src.assistant import grounding
from src.assistant.evals.grounding_repair_candidate_v7 import (
    POLICY_VERSION_V7,
    CanonicalCandidateV7,
    build_grounding_repair_candidate_v7,
)
from src.assistant.evals.grounding_repair_certificate_v7 import (
    CertificateDecisionV7,
    CertificateProfileV7,
    evaluate_grounding_repair_certificate_v7,
)
from src.assistant.evals.grounding_repair_v4_protocol import (
    AutomatedCaseReview,
    AutomatedReviewBatch,
    HistoricalExclusionManifest,
    HoldoutCase,
    HoldoutCollection,
    Preregistration,
    QuestionManifest,
    SelectionManifest,
    verify_v4,
)
from src.assistant.evals.grounding_repair_v4_replay import ReplayCache, replay_case_v4
from src.assistant.evals.grounding_repair_v4_replay_prepare import _validate_answer
from src.observability.payload_crypto import decrypt_payload

_SAFE_CODE_RE = re.compile(r"^[A-Za-z0-9_.:+-]+$")

@dataclass(frozen=True, slots=True)
class PrimedCandidateV7:
    """Plaintext-in-memory canonical projection prepared outside replay timing."""

    canonical: CanonicalCandidateV7
    candidate_error_codes: tuple[str, ...]


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _p95(values: list[float]) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    return ordered[max(0, math.ceil(0.95 * len(ordered)) - 1)]


def _safe_code(value: str | None) -> str:
    code = str(value or "none")
    return code if _SAFE_CODE_RE.fullmatch(code) else "unsafe_code"


def _band(value: int, *, cuts: tuple[int, ...]) -> str:
    lower = 0
    for upper in cuts:
        if value <= upper:
            return f"{lower:03d}-{upper:03d}"
        lower = upper + 1
    return f"{lower:03d}-plus"


def _payload_hash(value: Any) -> str:
    encoded = json.dumps(value, ensure_ascii=False, default=str).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _oracle_safe(review: AutomatedCaseReview, preregistration: Preregistration) -> bool:
    if not review.complete or review.candidate_score is None or review.baseline_score is None:
        return False
    gates = preregistration.gates
    return (
        review.candidate_score >= gates.candidate_minimum_score
        and not review.candidate_issue_codes
        and review.candidate_score
        >= review.baseline_score - gates.candidate_noninferiority_margin
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
        and preregistration.experiment_id
        == holdout.experiment_id
        == selection.experiment_id
        == reviews.experiment_id
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


def _payload_sources(payload: dict[str, Any]) -> list[dict[str, Any]]:
    return [item for item in payload.get("sources") or [] if isinstance(item, dict)]


def _payload_tool_results(payload: dict[str, Any]) -> list[dict[str, Any]]:
    return [item for item in payload.get("tool_results") or [] if isinstance(item, dict)]


def _prime_candidate_v7(payload: dict[str, Any]) -> PrimedCandidateV7:
    sources = _payload_sources(payload)
    tool_results = _payload_tool_results(payload)
    canonical = build_grounding_repair_candidate_v7(
        question=str(payload.get("question") or ""),
        sources=sources,
        tool_results=tool_results,
    )
    known_sources = frozenset(
        str(item.get("source_id") or "").strip()
        for item in sources
        if str(item.get("source_id") or "").strip()
    )
    factual_tools_used = any(item.get("tool") != "think" for item in tool_results)
    errors = (
        _validate_answer(canonical.candidate_answer, known_sources, factual_tools_used)
        if canonical.eligible
        else []
    )
    return PrimedCandidateV7(
        canonical=canonical,
        candidate_error_codes=tuple(
            grounding._validation_error_code(item) for item in errors
        ),
    )


def _replay_case_v7(
    payload: dict[str, Any],
    replay_cache: ReplayCache,
    primed: PrimedCandidateV7,
) -> tuple[dict[str, Any], CertificateDecisionV7, CertificateProfileV7]:
    decisions: list[CertificateDecisionV7] = []
    profiles: list[CertificateProfileV7] = []
    tool_results = _payload_tool_results(payload)

    def certificate_evaluator(**kwargs: Any) -> CertificateDecisionV7:
        profile = CertificateProfileV7()
        decision = evaluate_grounding_repair_certificate_v7(
            candidate_answer=primed.canonical.candidate_answer,
            initial_error_codes=kwargs.get("initial_error_codes") or (),
            candidate_error_codes=primed.candidate_error_codes,
            question=str(payload.get("question") or ""),
            sources=kwargs.get("sources") or (),
            tool_results=tool_results,
            canonical=primed.canonical,
            profile=profile,
        )
        decisions.append(decision)
        profiles.append(profile)
        return decision

    replay = replay_case_v4(
        payload,
        replay_cache,
        certificate_evaluator=certificate_evaluator,
    )
    if len(decisions) != 1 or len(profiles) != 1:
        raise RuntimeError("Policy-v7 replay did not produce exactly one decision")
    return replay, decisions[0], profiles[0]


def _validated_inputs(
    *,
    holdout: HoldoutCollection,
    selection: SelectionManifest,
    reviews: AutomatedReviewBatch,
    candidate_prime_times: list[float] | None = None,
) -> list[tuple[HoldoutCase, dict[str, Any], AutomatedCaseReview, PrimedCandidateV7]]:
    cases = {item.question_hash: item for item in holdout.cases}
    review_by_hash = {item.question_hash: item for item in reviews.reviews}
    prepared = []
    for question_hash, payload_hash in zip(
        selection.selected_question_hashes,
        selection.selected_case_payload_hashes,
        strict=True,
    ):
        case = cases.get(question_hash)
        review = review_by_hash.get(question_hash)
        if (
            case is None
            or review is None
            or case.case_payload_hash != payload_hash
            or review.case_payload_hash != payload_hash
        ):
            continue
        payload = _case_payload(case)
        if (
            payload is None
            or payload.get("question_hash") != case.question_hash
            or payload.get("request_hash") != case.request_hash
        ):
            continue
        started = time.perf_counter_ns()
        primed = _prime_candidate_v7(payload)
        if candidate_prime_times is not None:
            candidate_prime_times.append(
                (time.perf_counter_ns() - started) / 1_000_000
            )
        prepared.append((case, payload, review, primed))
    return prepared

def _evaluate_transformed_reviews(
    *,
    transformed_reviews: Any,
    selected: list[tuple[HoldoutCase, PrimedCandidateV7]],
    holdout: HoldoutCollection,
    selection: SelectionManifest,
    reviews: AutomatedReviewBatch,
    preregistration: Preregistration,
) -> tuple[str, bool, bool, int, float, int | None, int | None]:
    from src.assistant.evals.grounding_repair_v7_review import (
        TRANSFORMED_REVIEW_PROTOCOL_V7,
        decrypt_transformed_reviews_v7,
        transform_implementation_hash_v7,
        transformed_candidate_hash_v7,
        transformed_case_payload_hash_v7,
        transformed_prompt_identities_v7,
        transformed_review_identity_v7,
    )

    reference_hashes, judge_hash = transformed_prompt_identities_v7()
    expected_work = [
        (case.question_hash, str(case.case_payload_hash))
        for case, _primed in selected
        if case.case_payload_hash is not None
    ]
    lineage_valid = (
        verify_v4(transformed_reviews)
        and transformed_reviews.experiment_id == preregistration.experiment_id
        and transformed_reviews.policy_version == POLICY_VERSION_V7
        and transformed_reviews.review_protocol == TRANSFORMED_REVIEW_PROTOCOL_V7
        and transformed_reviews.source_holdout_hash == holdout.content_hash
        and transformed_reviews.source_selection_hash == selection.content_hash
        and transformed_reviews.source_review_hash == reviews.content_hash
        and transformed_reviews.source_model_identity_hash == preregistration.model_identity_hash
        and transformed_reviews.transformed_model_identity_hash
        == transformed_review_identity_v7(preregistration)
        and transformed_reviews.transformed_reference_prompt_identity_hashes == reference_hashes
        and transformed_reviews.transformed_judge_prompt_identity_hash == judge_hash
        and transformed_reviews.transform_implementation_hash == transform_implementation_hash_v7()
        and transformed_reviews.review_scope == "policy_v7_certificate_selected_cases"
        and transformed_reviews.source_selection_case_count == selection.selected_case_count
        and transformed_reviews.review_target_count == len(expected_work)
        and transformed_reviews.review_count == len(expected_work)
        and transformed_reviews.status == "complete"
        and transformed_reviews.research_only is True
        and transformed_reviews.serving_change_authorized is False
    )
    records_valid = lineage_valid
    resolved: list[AutomatedCaseReview] = []
    if records_valid:
        records = decrypt_transformed_reviews_v7(transformed_reviews)
        if len(records) != len(selected):
            records_valid = False
        else:
            for record, (case, primed) in zip(records, selected, strict=True):
                expected_candidate_hash = transformed_candidate_hash_v7(
                    primed.canonical.candidate_answer
                )
                expected_payload_hash = transformed_case_payload_hash_v7(
                    source_case_payload_hash=str(case.case_payload_hash),
                    transformed_candidate_hash=expected_candidate_hash,
                )
                if (
                    record.source_question_hash != case.question_hash
                    or record.source_case_payload_hash != case.case_payload_hash
                    or record.transformed_candidate_hash != expected_candidate_hash
                    or record.transformed_case_payload_hash != expected_payload_hash
                    or record.review.question_hash != case.question_hash
                    or record.review.case_payload_hash != expected_payload_hash
                    or record.review.model_identity_hash
                    != transformed_reviews.transformed_model_identity_hash
                ):
                    records_valid = False
                    break
                if record.review.complete:
                    resolved.append(record.review)
    complete_count = len(resolved)
    coverage = complete_count / len(selected) if selected else 0.0
    if not lineage_valid or not records_valid:
        return "invalid", lineage_valid, records_valid, complete_count, coverage, None, None
    if complete_count != len(selected):
        return "incomplete", True, True, complete_count, coverage, None, None
    false_positives = sum(
        int(not _oracle_safe(item, preregistration)) for item in resolved
    )
    issue_count = sum(len(item.candidate_issue_codes) for item in resolved)
    return "evaluated", True, True, complete_count, coverage, false_positives, issue_count

__all__ = [
    "PrimedCandidateV7",
    "_band",
    "_case_payload",
    "_evaluate_transformed_reviews",
    "_lineage_valid",
    "_oracle_safe",
    "_p95",
    "_prime_candidate_v7",
    "_replay_case_v7",
    "_safe_code",
    "_validated_inputs",
]
