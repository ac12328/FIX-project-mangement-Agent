"""Retrospective aggregate-only oracle ceiling for grounding-repair policy research."""

from __future__ import annotations

import statistics
from collections import Counter
from datetime import datetime, timezone
from typing import Any, Iterable

from src.assistant.evals.artifacts import content_hash, verify_content_hash
from src.assistant.evals.grounding_repair import (
    EvidenceOrigin,
    GroundingRepairGateReport,
    GroundingRepairReviewBatch,
    GroundingRepairShadowCohort,
    GroundingRepairShadowObservation,
    ReviewStatus,
    verify_grounding_repair_cohort_content_hash,
)
from src.assistant.evals.grounding_review_models import validate_review_lineage

_SOURCE_POLICY = "workspace-grounding-repair-shadow-v2"
_ORACLE_ANALYSIS_VERSION = "grounding-repair-policy-v3-retrospective-oracle-v1"
_NONINFERIOR_MARGIN_POINTS = 3.0
_REASON_TO_CLUSTER = {
    "candidate_semantic_support_unverified": "semantic_support_block",
    "too_many_initial_errors": "initial_error_budget_block",
    "substantive_retention_below_threshold": "retention_block",
}


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _share(numerator: int, denominator: int) -> float:
    return round(numerator / denominator, 6) if denominator else 0.0


def _percentile(values: list[float], percentile: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    index = max(0, min(len(ordered) - 1, round((len(ordered) - 1) * percentile)))
    return round(float(ordered[index]), 6)


def _numeric_summary(values: Iterable[float | int]) -> dict[str, float | int | None]:
    materialized = [float(value) for value in values]
    if not materialized:
        return {
            "count": 0,
            "min": None,
            "p50": None,
            "p95": None,
            "max": None,
            "mean": None,
        }
    return {
        "count": len(materialized),
        "min": round(min(materialized), 6),
        "p50": round(float(statistics.median(materialized)), 6),
        "p95": _percentile(materialized, 0.95),
        "max": round(max(materialized), 6),
        "mean": round(statistics.fmean(materialized), 6),
    }


def _counts(values: Iterable[str]) -> dict[str, int]:
    return dict(sorted(Counter(values).items()))


def _cluster_counts(
    rows: list[tuple[GroundingRepairShadowObservation, Any]],
) -> dict[str, int]:
    return _counts(_REASON_TO_CLUSTER[observation.reason] for observation, _ in rows)


def _validate_inputs(
    cohort: GroundingRepairShadowCohort,
    reviews: GroundingRepairReviewBatch,
    gate_report: GroundingRepairGateReport,
) -> list[GroundingRepairShadowObservation]:
    if not verify_grounding_repair_cohort_content_hash(cohort):
        raise ValueError("policy-v2 cohort content hash is invalid")
    if cohort.policy_version != _SOURCE_POLICY:
        raise ValueError(f"expected source policy {_SOURCE_POLICY}")
    validate_review_lineage(cohort, reviews)
    if any(item.status != ReviewStatus.REVIEWED for item in reviews.reviews):
        raise ValueError("oracle analysis requires completed reviews for every repair case")
    if not verify_content_hash(gate_report):
        raise ValueError("gate report content hash is invalid")
    if gate_report.cohort_id != cohort.cohort_id:
        raise ValueError("gate report cohort_id does not match cohort")
    if gate_report.cohort_hash != cohort.content_hash:
        raise ValueError("gate report cohort_hash does not match cohort")
    if gate_report.review_hash != reviews.content_hash:
        raise ValueError("gate report review_hash does not match review batch")
    if int(gate_report.metrics.get("projected_skip_case_count") or 0) != 0:
        raise ValueError("oracle ceiling requires a zero-projected-skip source policy")

    repair_cases = [
        item for item in cohort.observations if item.data_complete and item.baseline_repair_attempted
    ]
    unknown_reasons = sorted({item.reason for item in repair_cases} - set(_REASON_TO_CLUSTER))
    if unknown_reasons:
        raise ValueError("repair cohort contains unsupported first-blocking reasons")
    return repair_cases


def _oracle_segment(
    rows: list[tuple[GroundingRepairShadowObservation, Any]],
    *,
    repair_case_count: int,
) -> dict[str, Any]:
    savings = [
        observation.baseline_ready_ms - observation.candidate_ready_ms
        for observation, _ in rows
    ]
    score_differences = [
        float(review.candidate_score) - float(review.baseline_score)
        for _, review in rows
    ]
    candidate_issue_count = sum(len(review.candidate_issue_codes) for _, review in rows)
    return {
        "eligible_case_count": len(rows),
        "eligible_share_of_repair_cases": _share(len(rows), repair_case_count),
        "theoretical_repair_call_reduction_ceiling": _share(len(rows), repair_case_count),
        "cluster_counts": _cluster_counts(rows),
        "candidate_pass_count": sum(review.candidate_pass is True for _, review in rows),
        "baseline_pass_count": sum(review.baseline_pass is True for _, review in rows),
        "candidate_zero_issue_case_count": sum(
            not review.candidate_issue_codes for _, review in rows
        ),
        "candidate_issue_count": candidate_issue_count,
        "candidate_score": _numeric_summary(
            float(review.candidate_score) for _, review in rows
        ),
        "baseline_score": _numeric_summary(
            float(review.baseline_score) for _, review in rows
        ),
        "paired_candidate_minus_baseline_score": _numeric_summary(score_differences),
        "theoretical_completion_savings_ms": _numeric_summary(savings),
    }


def analyze_policy_v3_retrospective_oracle_ceiling(
    cohort: GroundingRepairShadowCohort,
    reviews: GroundingRepairReviewBatch,
    gate_report: GroundingRepairGateReport,
    *,
    generated_at: str | None = None,
) -> dict[str, Any]:
    """Estimate an outcome-aware ceiling without creating a runtime selector."""

    repair_cases = _validate_inputs(cohort, reviews, gate_report)
    observations = {item.case_id: item for item in repair_cases}
    joined = [(observations[item.case_id], item) for item in reviews.reviews]
    strict_rows = [
        row
        for row in joined
        if row[1].candidate_pass is True and not row[1].candidate_issue_codes
    ]
    noninferior_rows = [
        row
        for row in joined
        if not row[1].candidate_issue_codes
        and float(row[1].candidate_score)
        >= float(row[1].baseline_score) - _NONINFERIOR_MARGIN_POINTS
    ]
    controls = [
        item for item in cohort.observations if item.data_complete and not item.baseline_repair_attempted
    ]
    strict = _oracle_segment(strict_rows, repair_case_count=len(repair_cases))
    noninferior = _oracle_segment(noninferior_rows, repair_case_count=len(repair_cases))

    artifact: dict[str, Any] = {
        "schema_version": 1,
        "purpose": "grounding_repair_policy_v3_retrospective_oracle_ceiling",
        "analysis_version": _ORACLE_ANALYSIS_VERSION,
        "generated_at": generated_at or _utc_now(),
        "cohort_id": cohort.cohort_id,
        "source_policy_version": cohort.policy_version,
        "analysis_class": "retrospective_offline_outcome_oracle",
        "lineage": {
            "cohort_hash": cohort.content_hash,
            "review_hash": reviews.content_hash,
            "gate_report_hash": gate_report.content_hash,
        },
        "scope": {
            "qualifying_observation_count": len(repair_cases) + len(controls),
            "repair_case_count": len(repair_cases),
            "no_repair_control_count": len(controls),
            "completed_review_count": len(reviews.reviews),
            "repair_review_coverage": _share(len(reviews.reviews), len(repair_cases)),
            "source_policy_projected_skip_case_count": 0,
            "source_policy_projected_repair_call_reduction": 0.0,
            "organic_real_user_case_count": sum(
                item.origin == EvidenceOrigin.REAL for item in cohort.observations
            ),
            "collection_origin_counts": _counts(
                item.origin.value for item in cohort.observations
            ),
        },
        "strict_review_oracle": {
            "definition": {
                "candidate_pass_required": True,
                "candidate_issue_count_required": 0,
                "uses_post_hoc_review_outcomes": True,
            },
            **strict,
        },
        "noninferiority_review_oracle": {
            "definition": {
                "candidate_score_floor": "baseline_score_minus_margin",
                "noninferiority_margin_points": _NONINFERIOR_MARGIN_POINTS,
                "candidate_issue_count_required": 0,
                "uses_post_hoc_review_outcomes": True,
            },
            **noninferior,
        },
        "interpretation": {
            "strict_ceiling": (
                "The strict oracle is the maximum observed repair reduction if an impossible "
                "selector already knew the completed Candidate review outcome before serving."
            ),
            "noninferiority_ceiling": (
                "The noninferiority oracle applies the existing three-point score margin and "
                "requires zero Candidate safety issues."
            ),
            "latency_ceiling": (
                "Completion savings are retrospective Candidate-ready minus Baseline-ready "
                "differences, not measured user-visible savings from a served policy."
            ),
        },
        "oracle_limitations": [
            "uses_post_hoc_labels_unavailable_to_runtime",
            "does_not_define_an_implementable_selector",
            "does_not_measure_holdout_false_positive_rate",
            "controlled_simulation_only",
            "organic_real_user_evidence_absent",
            "cannot_authorize_runtime_or_serving_changes",
        ],
        "predeclared_next_experiment_gates": {
            "deterministic_hash_split_required": True,
            "plaintext_features_forbidden": True,
            "per_case_feature_rows_persisted": False,
            "holdout_false_positive_count_must_equal": 0,
            "selected_candidate_issue_count_must_equal": 0,
            "shadow_compute_p95_must_be_below_ms": 5.0,
            "runtime_wiring_authorized": False,
            "serving_change_authorized": False,
            "promotion_requires_organic_evidence": True,
        },
        "decision": {
            "status": "oracle_ceiling_measured_not_implementable",
            "keep_shadow_only": True,
            "policy_change_implemented": False,
            "runtime_selector_implemented": False,
            "serving_change_implemented": False,
            "promotion_authorized": False,
            "human_review_required_for_current_decision": False,
            "recommended_current_action": "run_offline_hash_split_selector_experiment",
        },
        "privacy": {
            "plaintext_persisted": False,
            "per_case_records_persisted": False,
            "answers_persisted": False,
            "questions_persisted": False,
            "source_content_persisted": False,
            "aggregate_metadata_only": True,
        },
    }
    artifact["content_hash"] = content_hash(artifact)
    return artifact


__all__ = ["analyze_policy_v3_retrospective_oracle_ceiling"]
