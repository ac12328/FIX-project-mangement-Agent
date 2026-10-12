"""Aggregate-only post-hoc development runner for grounding-repair Policy-v5.

The runner reuses the sealed Policy-v4 R6 holdout solely as development evidence.
Decrypted case payloads and typed source projections stay in memory. Durable output
contains aggregate counts, timings, gate results, and artifact-level lineage only.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
import time
from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any, Literal

from pydantic import Field, model_validator

from src.assistant.evals.artifacts import read_model, write_model
from src.assistant import grounding
from src.assistant.evals.grounding_repair_candidate_v5 import (
    CandidateFilterResultV5,
    filter_grounding_repair_candidate_v5,
)
from src.assistant.evals.grounding_repair_certificate_v5 import (
    CertificateDecisionV5,
    CertificateProfileV5,
    evaluate_grounding_repair_certificate_v5,
)
from src.assistant.evals.grounding_repair_certificate_v5_projection import (
    ProjectionCacheV5,
)
from src.assistant.evals.grounding_repair_certificate_v5_rules import (
    CITATION_RE,
    LineContractCacheV5,
    POLICY_VERSION,
)
from src.assistant.evals.grounding_repair_v4_protocol import (
    AutomatedCaseReview,
    AutomatedReviewBatch,
    HistoricalExclusionManifest,
    HoldoutCase,
    HoldoutCollection,
    POLICY_V4,
    Preregistration,
    QuestionManifest,
    SelectionManifest,
    V4Model,
    stamp_v4,
    verify_v4,
)
from src.assistant.evals.grounding_repair_v4_replay import (
    ReplayCache,
    prime_replay_cache,
    replay_case_v4,
)
from src.assistant.evals.grounding_repair_v4_replay_prepare import _validate_answer
from src.observability.payload_crypto import decrypt_payload

_SAFE_CODE_RE = re.compile(r"^[A-Za-z0-9_.:+-]+$")


class DevelopmentStatusV5(str, Enum):
    """Outcome of an offline post-hoc Policy-v5 development pass."""

    PASSED = "posthoc_development_gates_passed_not_confirmation"
    FAILED_SAFETY = "posthoc_development_failed_safety"
    INCONCLUSIVE = "posthoc_development_inconclusive_or_utility_failed"
    INCOMPLETE = "posthoc_development_incomplete"


class DevelopmentReportV5(V4Model):
    """Plaintext-free, aggregate-only Policy-v5 development evidence."""

    schema_version: Literal[1] = 1
    experiment_id: str = Field(min_length=3)
    generated_at: datetime
    status: DevelopmentStatusV5
    policy_version: Literal[POLICY_VERSION] = POLICY_VERSION
    source_policy_version: Literal[POLICY_V4] = POLICY_V4
    source_preregistration_hash: str = Field(min_length=8)
    source_question_manifest_hash: str = Field(min_length=8)
    source_historical_exclusion_hash: str = Field(min_length=8)
    source_holdout_hash: str = Field(min_length=8)
    source_selection_hash: str = Field(min_length=8)
    source_review_hash: str = Field(min_length=8)
    transformed_review_hash: str | None = Field(default=None, min_length=8)
    transformed_review_status: Literal[
        "evaluated", "unavailable", "incomplete", "invalid"
    ] = "unavailable"
    metrics: dict[str, int | float | bool | None]
    gates: dict[str, bool]
    decision_reason_counts: dict[str, int]
    risk_code_counts: dict[str, int]
    posthoc: Literal[True] = True
    confirmation_evidence: Literal[False] = False
    research_only: Literal[True] = True
    serving_change_authorized: Literal[False] = False
    content_hash: str = ""

    @model_validator(mode="after")
    def validate_aggregate_contract(self) -> "DevelopmentReportV5":
        if any(value < 0 for value in self.decision_reason_counts.values()):
            raise ValueError("decision reason counts cannot be negative")
        if any(value < 0 for value in self.risk_code_counts.values()):
            raise ValueError("risk code counts cannot be negative")
        return self


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


def _payload_hash(value: Any) -> str:
    plaintext = json.dumps(value, ensure_ascii=False, default=str).encode("utf-8")
    return hashlib.sha256(plaintext).hexdigest()


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
    """Decrypt and validate one selected case without returning durable plaintext."""

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


def _cited_source_ids(payload: dict[str, Any]) -> frozenset[str]:
    material = "\n".join((
        str(payload.get("prepared_answer") or ""),
        str(payload.get("candidate_answer") or ""),
    ))
    return frozenset(CITATION_RE.findall(material))


def _prime_projection_cache_v5(payload: dict[str, Any]) -> ProjectionCacheV5:
    """Preindex one case and materialize only cited sources outside replay timing."""

    cache = ProjectionCacheV5.build(
        _payload_sources(payload),
        _payload_tool_results(payload),
    )
    cache.materialize(_cited_source_ids(payload))
    return cache


def _prime_contract_cache_v5(
    payload: dict[str, Any],
    projection_cache: ProjectionCacheV5,
    contract_cache: LineContractCacheV5,
) -> "PrimedCandidateV5":
    """Prepare the deterministic transformed candidate outside replay timing."""

    sources = _payload_sources(payload)
    tool_results = _payload_tool_results(payload)
    filtered = filter_grounding_repair_candidate_v5(
        candidate_answer=str(payload.get("candidate_answer") or ""),
        question=str(payload.get("question") or ""),
        sources=sources,
        tool_results=tool_results,
        projection_cache=projection_cache,
        contract_cache=contract_cache,
    )
    known_sources = frozenset(
        str(item.get("source_id") or "").strip()
        for item in sources
        if str(item.get("source_id") or "").strip()
    )
    factual_tools_used = any(item.get("tool") != "think" for item in tool_results)
    errors = _validate_answer(filtered.candidate_answer, known_sources, factual_tools_used)
    return PrimedCandidateV5(
        source_candidate_answer=str(payload.get("candidate_answer") or ""),
        filtered=filtered,
        candidate_error_codes=tuple(
            grounding._validation_error_code(item) for item in errors
        ),
    )


@dataclass(frozen=True, slots=True)
class PrimedCandidateV5:
    """Plaintext-in-memory deterministic transformation prepared before replay timing."""

    source_candidate_answer: str
    filtered: CandidateFilterResultV5
    candidate_error_codes: tuple[str, ...]


def _replay_case_v5(
    payload: dict[str, Any],
    replay_cache: ReplayCache,
    projection_cache: ProjectionCacheV5,
    contract_cache: LineContractCacheV5 | None = None,
    primed_candidate: PrimedCandidateV5 | None = None,
) -> tuple[dict[str, Any], CertificateDecisionV5, CertificateProfileV5]:
    decision_holder: list[CertificateDecisionV5] = []
    profile_holder: list[CertificateProfileV5] = []
    tool_results = _payload_tool_results(payload)

    def certificate_evaluator(**kwargs: Any) -> CertificateDecisionV5:
        profile = CertificateProfileV5()
        source_records = [
            item for item in kwargs.get("sources") or () if isinstance(item, dict)
        ]
        factual_tools_used = bool(kwargs.get("factual_tools_used"))
        filter_started = time.perf_counter_ns()
        candidate_answer = str(kwargs.get("candidate_answer") or "")
        use_primed = (
            primed_candidate is not None
            and primed_candidate.source_candidate_answer == candidate_answer
        )
        if use_primed:
            filtered = primed_candidate.filtered
            filtered_error_codes = primed_candidate.candidate_error_codes
            profile.candidate_filter_strategy = "deterministic_transform_primed"
        else:
            filtered = filter_grounding_repair_candidate_v5(
                candidate_answer=candidate_answer,
                question=str(payload.get("question") or ""),
                sources=source_records,
                tool_results=tool_results,
                projection_cache=projection_cache,
                contract_cache=contract_cache,
            )
            known_sources = frozenset(
                str(item.get("source_id") or "").strip()
                for item in source_records
                if str(item.get("source_id") or "").strip()
            )
            filtered_errors = _validate_answer(
                filtered.candidate_answer,
                known_sources,
                factual_tools_used,
            )
            filtered_error_codes = tuple(
                grounding._validation_error_code(item) for item in filtered_errors
            )
        profile.candidate_filter_compute_ns = time.perf_counter_ns() - filter_started
        profile.candidate_filter_input_line_count = filtered.input_line_count
        profile.candidate_filter_retained_line_count = filtered.retained_line_count
        profile.candidate_filter_dropped_factual_line_count = (
            filtered.dropped_factual_line_count
        )
        decision = evaluate_grounding_repair_certificate_v5(
            prepared_answer=str(kwargs.get("prepared_answer") or ""),
            candidate_answer=filtered.candidate_answer,
            initial_error_codes=kwargs.get("initial_error_codes") or (),
            candidate_error_codes=filtered_error_codes,
            sources=source_records,
            factual_tools_used=factual_tools_used,
            question=str(payload.get("question") or ""),
            tool_results=tool_results,
            projection_cache=projection_cache,
            contract_cache=contract_cache,
            profile=profile,
        )
        decision_holder.append(decision)
        profile_holder.append(profile)
        return decision

    replay = replay_case_v4(
        payload,
        replay_cache,
        certificate_evaluator=certificate_evaluator,
    )
    if len(decision_holder) != 1 or len(profile_holder) != 1:
        raise RuntimeError("Policy-v5 replay did not produce exactly one certificate decision")
    return replay, decision_holder[0], profile_holder[0]


def derive_certificate_selected_work_v5(
    *,
    historical: HistoricalExclusionManifest,
    manifest: QuestionManifest,
    preregistration: Preregistration,
    holdout: HoldoutCollection,
    selection: SelectionManifest,
    reviews: AutomatedReviewBatch,
) -> list[tuple[str, str]]:
    """Return the deterministic certificate-selected worklist in memory only."""

    artifacts = (historical, manifest, preregistration, holdout, selection, reviews)
    if not all(verify_v4(item) for item in artifacts):
        raise ValueError("Policy-v5 worklist source artifact hash is invalid")
    if not _lineage_valid(
        historical=historical,
        manifest=manifest,
        preregistration=preregistration,
        holdout=holdout,
        selection=selection,
        reviews=reviews,
    ):
        raise ValueError("Policy-v5 worklist source lineage is invalid")

    cases_by_hash = {item.question_hash: item for item in holdout.cases}
    reviews_by_hash = {item.question_hash: item for item in reviews.reviews}
    prepared: list[tuple[HoldoutCase, dict[str, Any], ProjectionCacheV5]] = []
    for question_hash, payload_hash in zip(
        selection.selected_question_hashes,
        selection.selected_case_payload_hashes,
        strict=True,
    ):
        case = cases_by_hash.get(question_hash)
        review = reviews_by_hash.get(question_hash)
        if (
            case is None
            or review is None
            or case.case_payload_hash != payload_hash
            or review.case_payload_hash != payload_hash
        ):
            raise ValueError("Policy-v5 worklist case lineage is invalid")
        payload = _case_payload(case)
        if (
            payload is None
            or payload.get("question_hash") != case.question_hash
            or payload.get("request_hash") != case.request_hash
        ):
            raise ValueError("Policy-v5 worklist encrypted payload is invalid")
        prepared.append((case, payload, _prime_projection_cache_v5(payload)))

    replay_cache = ReplayCache()
    contract_cache = LineContractCacheV5()
    prime_replay_cache([payload for _case, payload, _cache in prepared], replay_cache)
    selected: list[tuple[str, str]] = []
    for case, payload, projection_cache in prepared:
        primed_candidate = _prime_contract_cache_v5(
            payload, projection_cache, contract_cache
        )
        replay, decision, _profile = _replay_case_v5(
            payload,
            replay_cache,
            projection_cache,
            contract_cache,
            primed_candidate,
        )
        if not (
            replay["candidate_parity"]
            and replay["initial_error_parity"]
            and replay["candidate_error_parity"]
        ):
            raise ValueError("Policy-v5 worklist replay parity failed")
        if decision.would_skip_repair:
            if case.case_payload_hash is None:
                raise ValueError("Policy-v5 selected case payload hash is missing")
            selected.append((case.question_hash, case.case_payload_hash))
    return selected


def run_development_v5(
    *,
    historical: HistoricalExclusionManifest,
    manifest: QuestionManifest,
    preregistration: Preregistration,
    holdout: HoldoutCollection,
    selection: SelectionManifest,
    reviews: AutomatedReviewBatch,
    transformed_reviews: Any | None = None,
    output_path: Path | None = None,
    generated_at: datetime | None = None,
) -> DevelopmentReportV5:
    """Run aggregate-only Policy-v5 diagnostics against a sealed Policy-v4 holdout."""

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
    selected_cases: list[HoldoutCase] = []
    complete_reviews = 0
    replay_inputs: list[
        tuple[
            HoldoutCase,
            dict[str, Any],
            AutomatedCaseReview,
            ProjectionCacheV5,
            PrimedCandidateV5 | None,
        ]
    ] = []
    projection_prime_times: list[float] = []
    contract_prime_times: list[float] = []

    if artifacts_valid and lineage_valid:
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
            started = time.perf_counter_ns()
            projection_cache = _prime_projection_cache_v5(payload)
            projection_prime_times.append((time.perf_counter_ns() - started) / 1_000_000)
            replay_inputs.append((case, payload, review, projection_cache, None))

    replay_cache = ReplayCache()
    contract_cache = LineContractCacheV5()
    prime_replay_cache([payload for _, payload, _, _, _ in replay_inputs], replay_cache)
    primed_replay_inputs = []
    for case, payload, review, projection_cache, _ in replay_inputs:
        started = time.perf_counter_ns()
        primed_candidate = _prime_contract_cache_v5(
            payload, projection_cache, contract_cache
        )
        contract_prime_times.append((time.perf_counter_ns() - started) / 1_000_000)
        primed_replay_inputs.append(
            (case, payload, review, projection_cache, primed_candidate)
        )
    replay_inputs = primed_replay_inputs

    replay_count = candidate_parity = initial_parity = candidate_error_parity = 0
    selected_count = 0
    selected_transformed: list[tuple[HoldoutCase, PrimedCandidateV5]] = []
    certificate_times: list[float] = []
    full_times: list[float] = []
    projection_lookup_times: list[float] = []
    decision_times: list[float] = []
    candidate_filter_times: list[float] = []
    candidate_filter_dropped_counts: list[float] = []
    candidate_filter_retained_counts: list[float] = []
    materialized_counts: list[float] = []
    decision_reasons: Counter[str] = Counter()
    risk_codes: Counter[str] = Counter()

    primed_filter_count = 0
    for _case, payload, review, projection_cache, primed_candidate in replay_inputs:
        try:
            replay, decision, profile = _replay_case_v5(
                payload, replay_cache, projection_cache, contract_cache, primed_candidate
            )
        except Exception:
            decision_reasons["replay_exception"] += 1
            continue
        replay_count += 1
        candidate_parity += int(replay["candidate_parity"])
        initial_parity += int(replay["initial_error_parity"])
        candidate_error_parity += int(replay["candidate_error_parity"])
        certificate_times.append(float(replay["certificate_ms"]))
        full_times.append(float(replay["full_replay_ms"]))
        projection_lookup_times.append(profile.projection_compute_ms)
        decision_times.append(profile.decision_compute_ms)
        candidate_filter_times.append(
            float(getattr(profile, "candidate_filter_compute_ms", 0.0))
        )
        primed_filter_count += int(
            getattr(profile, "candidate_filter_strategy", "")
            == "deterministic_transform_primed"
        )
        candidate_filter_dropped_counts.append(float(
            getattr(profile, "candidate_filter_dropped_factual_line_count", 0)
        ))
        candidate_filter_retained_counts.append(float(
            getattr(profile, "candidate_filter_retained_line_count", 0)
        ))
        materialized_counts.append(float(profile.projection_materialized_count))
        decision_reasons[_safe_code(decision.reason)] += 1
        risk_codes.update(_safe_code(code) for code in decision.risk_codes)
        if decision.would_skip_repair:
            selected_count += 1
            selected_transformed.append((_case, primed_candidate))

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

    transformed_review_hash: str | None = None
    transformed_review_status = "unavailable"
    transformed_review_complete_count = 0
    transformed_review_coverage = 0.0
    false_positives: int | None = None
    selected_issue_count: int | None = None
    transformed_lineage_valid = False
    transformed_records_valid = False
    if transformed_reviews is not None:
        transformed_review_hash = (
            str(getattr(transformed_reviews, "content_hash", "") or "") or None
        )
        try:
            from src.assistant.evals.grounding_repair_v5_review import (
                TRANSFORMED_REVIEW_PROTOCOL_V5,
                decrypt_transformed_reviews_v5,
                transform_implementation_hash_v5,
                transformed_candidate_hash_v5,
                transformed_case_payload_hash_v5,
                transformed_prompt_identities_v5,
                transformed_review_identity_v5,
            )

            reference_hashes, judge_hash = transformed_prompt_identities_v5()
            selected_work = [
                (case.question_hash, str(case.case_payload_hash))
                for case, _primed_candidate in selected_transformed
                if case.case_payload_hash is not None
            ]
            full_work = list(zip(
                selection.selected_question_hashes,
                selection.selected_case_payload_hashes,
                strict=True,
            ))
            scope = transformed_reviews.review_scope
            if scope == "policy_v5_certificate_selected_cases":
                expected_work = selected_work
            elif scope == "all_selected_v4_cases":
                expected_work = full_work
            else:
                expected_work = []
            transformed_lineage_valid = bool(
                verify_v4(transformed_reviews)
                and transformed_reviews.experiment_id == preregistration.experiment_id
                and transformed_reviews.policy_version == POLICY_VERSION
                and transformed_reviews.review_protocol == TRANSFORMED_REVIEW_PROTOCOL_V5
                and transformed_reviews.source_holdout_hash == holdout.content_hash
                and transformed_reviews.source_selection_hash == selection.content_hash
                and transformed_reviews.source_review_hash == reviews.content_hash
                and transformed_reviews.source_model_identity_hash
                == preregistration.model_identity_hash
                and transformed_reviews.transformed_model_identity_hash
                == transformed_review_identity_v5(preregistration)
                and transformed_reviews.transformed_reference_prompt_identity_hashes
                == reference_hashes
                and transformed_reviews.transformed_judge_prompt_identity_hash
                == judge_hash
                and transformed_reviews.transform_implementation_hash
                == transform_implementation_hash_v5()
                and transformed_reviews.source_selection_case_count
                == selection.selected_case_count
                and transformed_reviews.review_target_count == len(expected_work)
                and transformed_reviews.review_count == len(expected_work)
                and transformed_reviews.status == "complete"
                and transformed_reviews.research_only is True
                and transformed_reviews.serving_change_authorized is False
            )
            records = (
                decrypt_transformed_reviews_v5(transformed_reviews)
                if transformed_lineage_valid
                else []
            )
            actual_work = [
                (item.source_question_hash, item.source_case_payload_hash)
                for item in records
            ]
            transformed_records_valid = actual_work == expected_work
            record_by_hash = {item.source_question_hash: item for item in records}
            resolved_reviews: list[AutomatedCaseReview] = []
            if transformed_records_valid and len(record_by_hash) == len(records):
                for case, primed_candidate in selected_transformed:
                    record = record_by_hash.get(case.question_hash)
                    if record is None or case.case_payload_hash is None:
                        transformed_records_valid = False
                        break
                    expected_candidate_hash = transformed_candidate_hash_v5(
                        primed_candidate.filtered.candidate_answer
                    )
                    expected_payload_hash = transformed_case_payload_hash_v5(
                        source_case_payload_hash=case.case_payload_hash,
                        transformed_candidate_hash=expected_candidate_hash,
                    )
                    if (
                        record.source_case_payload_hash != case.case_payload_hash
                        or record.transformed_candidate_hash != expected_candidate_hash
                        or record.transformed_case_payload_hash != expected_payload_hash
                        or record.review.question_hash != case.question_hash
                        or record.review.case_payload_hash != expected_payload_hash
                        or record.review.model_identity_hash
                        != transformed_reviews.transformed_model_identity_hash
                    ):
                        transformed_records_valid = False
                        break
                    if record.review.complete:
                        resolved_reviews.append(record.review)
            else:
                transformed_records_valid = False
            transformed_review_complete_count = len(resolved_reviews)
            transformed_review_coverage = (
                transformed_review_complete_count / selected_count
                if selected_count
                else 0.0
            )
            if transformed_lineage_valid and transformed_records_valid:
                if transformed_review_complete_count == selected_count:
                    transformed_review_status = "evaluated"
                    false_positives = sum(
                        int(not _oracle_safe(item, preregistration))
                        for item in resolved_reviews
                    )
                    selected_issue_count = sum(
                        len(item.candidate_issue_codes) for item in resolved_reviews
                    )
                else:
                    transformed_review_status = "incomplete"
            else:
                transformed_review_status = "invalid"
        except Exception:
            transformed_review_status = "invalid"

    gates = {
        "input_artifacts_valid": artifacts_valid,
        "lineage_valid": lineage_valid,
        "selection_complete": (
            selection.status == "complete" and selection.selected_case_count == target
        ),
        "complete_repair_case_target_met": complete_repair_count == target,
        "distinct_request_target_met": (
            distinct_request_count == preregistration.gates.distinct_request_target
        ),
        "automated_review_coverage_met": (
            review_coverage >= preregistration.gates.automated_review_coverage
        ),
        "transformed_review_lineage_valid": transformed_lineage_valid,
        "transformed_review_records_valid": transformed_records_valid,
        "transformed_review_coverage_met": (
            transformed_review_status == "evaluated"
            and transformed_review_coverage >= preregistration.gates.automated_review_coverage
        ),
        "replay_candidate_and_error_parity": replay_parity,
        "minimum_certificate_selected_cases_met": (
            selected_count >= preregistration.gates.minimum_selected_cases
        ),
        "minimum_projected_repair_reduction_met": (
            projected_reduction
            >= preregistration.gates.minimum_projected_repair_reduction
        ),
        "false_positive_limit_met": (
            false_positives is not None
            and false_positives <= preregistration.gates.maximum_false_positives
        ),
        "selected_candidate_issue_limit_met": (
            selected_issue_count is not None
            and selected_issue_count
            <= preregistration.gates.maximum_selected_candidate_issue_count
        ),
        "certificate_p95_met": (
            certificate_p95 < preregistration.gates.maximum_certificate_p95_ms
        ),
        "full_replay_p95_met": (
            full_replay_p95 < preregistration.gates.maximum_full_replay_p95_ms
        ),
    }
    incomplete_names = (
        "input_artifacts_valid",
        "lineage_valid",
        "selection_complete",
        "complete_repair_case_target_met",
        "distinct_request_target_met",
        "automated_review_coverage_met",
        "transformed_review_lineage_valid",
        "transformed_review_records_valid",
        "transformed_review_coverage_met",
        "replay_candidate_and_error_parity",
    )
    safety_names = (
        "false_positive_limit_met",
        "selected_candidate_issue_limit_met",
    )
    if not all(gates[name] for name in incomplete_names):
        status = DevelopmentStatusV5.INCOMPLETE
    elif not all(gates[name] for name in safety_names):
        status = DevelopmentStatusV5.FAILED_SAFETY
    elif not all(gates.values()):
        status = DevelopmentStatusV5.INCONCLUSIVE
    else:
        status = DevelopmentStatusV5.PASSED

    metrics: dict[str, int | float | bool | None] = {
        "holdout_case_count": len(holdout.cases),
        "complete_repair_case_count": complete_repair_count,
        "selected_case_count": selection.selected_case_count,
        "distinct_request_hash_count": distinct_request_count,
        "automated_review_complete_count": complete_reviews,
        "automated_review_coverage": review_coverage,
        "transformed_review_complete_count": transformed_review_complete_count,
        "transformed_review_coverage": transformed_review_coverage,
        "decrypted_selected_case_count": len(replay_inputs),
        "replay_complete_count": replay_count,
        "candidate_answer_parity_count": candidate_parity,
        "initial_error_parity_count": initial_parity,
        "candidate_error_parity_count": candidate_error_parity,
        "certificate_selected_count": selected_count,
        "projected_repair_reduction": projected_reduction,
        "false_positive_count": false_positives,
        "selected_candidate_issue_count": selected_issue_count,
        "projection_prime_p95_ms": _p95(projection_prime_times),
        "candidate_contract_prime_p95_ms": _p95(contract_prime_times),
        "candidate_transform_prime_p95_ms": _p95(contract_prime_times),
        "candidate_transform_primed_replay_count": primed_filter_count,
        "projection_lookup_p95_ms": _p95(projection_lookup_times),
        "candidate_filter_p95_ms": _p95(candidate_filter_times),
        "candidate_filter_dropped_factual_line_p95_count": _p95(
            candidate_filter_dropped_counts
        ),
        "candidate_filter_retained_line_p95_count": _p95(
            candidate_filter_retained_counts
        ),
        "certificate_decision_p95_ms": _p95(decision_times),
        "certificate_p95_ms": certificate_p95,
        "full_replay_p95_ms": full_replay_p95,
        "projection_materialized_p95_count": _p95(materialized_counts),
    }
    report = stamp_v4(DevelopmentReportV5(
        experiment_id=preregistration.experiment_id,
        generated_at=generated_at or _utcnow(),
        status=status,
        source_preregistration_hash=preregistration.content_hash,
        source_question_manifest_hash=manifest.content_hash,
        source_historical_exclusion_hash=historical.content_hash,
        source_holdout_hash=holdout.content_hash,
        source_selection_hash=selection.content_hash,
        source_review_hash=reviews.content_hash,
        transformed_review_hash=transformed_review_hash,
        transformed_review_status=transformed_review_status,
        metrics=metrics,
        gates=gates,
        decision_reason_counts=dict(sorted(decision_reasons.items())),
        risk_code_counts=dict(sorted(risk_codes.items())),
    ))
    if output_path is not None:
        write_model(Path(output_path), report)
    return report



def _read_transformed_reviews_v5(path: Path) -> Any:
    from src.assistant.evals.grounding_repair_v5_review import (
        TransformedReviewArtifactV5,
    )

    return read_model(Path(path), TransformedReviewArtifactV5)

def run_development_from_paths_v5(
    *,
    artifact_dir: Path,
    output_path: Path,
    transformed_reviews_path: Path | None = None,
    generated_at: datetime | None = None,
) -> DevelopmentReportV5:
    """Load a sealed Policy-v4 artifact directory and write a Policy-v5 report."""

    root = Path(artifact_dir)
    return run_development_v5(
        historical=read_model(root / "historical-exclusions.json", HistoricalExclusionManifest),
        manifest=read_model(root / "question-manifest.json", QuestionManifest),
        preregistration=read_model(root / "preregistration.json", Preregistration),
        holdout=read_model(root / "holdout.json", HoldoutCollection),
        selection=read_model(root / "selection.json", SelectionManifest),
        reviews=read_model(root / "reviews.json", AutomatedReviewBatch),
        transformed_reviews=(
            _read_transformed_reviews_v5(transformed_reviews_path)
            if transformed_reviews_path is not None and Path(transformed_reviews_path).exists()
            else None
        ),
        output_path=output_path,
        generated_at=generated_at,
    )


__all__ = [
    "DevelopmentReportV5",
    "DevelopmentStatusV5",
    "derive_certificate_selected_work_v5",
    "run_development_from_paths_v5",
    "run_development_v5",
]
