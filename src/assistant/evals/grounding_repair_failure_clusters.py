"""Plaintext-free failure-cluster analysis for grounding-repair shadow policy v2."""

from __future__ import annotations

import hashlib
import json
import statistics
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from src.assistant.evals.artifacts import content_hash, verify_content_hash
from src.assistant.evals.grounding_repair import (
    EvidenceOrigin,
    GroundingRepairGateReport,
    GroundingRepairShadowCohort,
    GroundingRepairShadowObservation,
    verify_grounding_repair_cohort_content_hash,
)

_POLICY_V2 = "workspace-grounding-repair-shadow-v2"
_SEMANTIC_REASON = "candidate_semantic_support_unverified"
_TOO_MANY_REASON = "too_many_initial_errors"
_RETENTION_REASON = "substantive_retention_below_threshold"
_EXPECTED_ZERO_SKIP_REASONS = frozenset({
    _SEMANTIC_REASON,
    _TOO_MANY_REASON,
    _RETENTION_REASON,
})


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _case_id_hash(value: str) -> str:
    return "sha256:" + hashlib.sha256(value.encode("utf-8")).hexdigest()


def _percentile(values: list[float], percentile: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    index = max(0, min(len(ordered) - 1, round((len(ordered) - 1) * percentile)))
    return round(float(ordered[index]), 6)


def _numeric_summary(values: Iterable[float | int | None]) -> dict[str, float | int | None]:
    materialized = [float(value) for value in values if value is not None]
    if not materialized:
        return {"count": 0, "min": None, "p50": None, "p95": None, "max": None, "mean": None}
    return {
        "count": len(materialized),
        "min": round(min(materialized), 6),
        "p50": _percentile(materialized, 0.50),
        "p95": _percentile(materialized, 0.95),
        "max": round(max(materialized), 6),
        "mean": round(statistics.fmean(materialized), 6),
    }


def _counts(values: Iterable[Any]) -> dict[str, int]:
    return dict(sorted(Counter(str(value) for value in values).items()))


def _share(numerator: int, denominator: int) -> float:
    return round(numerator / denominator, 6) if denominator else 0.0


def _cluster_metrics(
    observations: list[GroundingRepairShadowObservation],
    *,
    repair_case_count: int,
) -> dict[str, Any]:
    return {
        "case_count": len(observations),
        "share_of_repair_cases": _share(len(observations), repair_case_count),
        "initial_error_count": _numeric_summary(
            len(item.initial_error_codes) for item in observations
        ),
        "initial_error_code_counts": _counts(
            code for item in observations for code in item.initial_error_codes
        ),
        "substantive_line_retention": _numeric_summary(
            item.substantive_line_retention for item in observations
        ),
        "candidate_ready_ms": _numeric_summary(
            item.candidate_ready_ms for item in observations
        ),
        "baseline_ready_ms": _numeric_summary(
            item.baseline_ready_ms for item in observations
        ),
        "shadow_compute_ms": _numeric_summary(
            item.shadow_compute_ms for item in observations
        ),
        "semantic_line_count": _numeric_summary(
            item.semantic_line_count for item in observations
        ),
        "semantic_lines_evaluated": _numeric_summary(
            item.semantic_lines_evaluated for item in observations
        ),
        "semantic_support_materialized_count": _numeric_summary(
            item.semantic_support_materialized_count for item in observations
        ),
        "semantic_short_circuit_count": sum(
            item.semantic_short_circuit is True for item in observations
        ),
        "semantic_analysis_complete_count": sum(
            item.semantic_analysis_complete is True for item in observations
        ),
    }


def _validate_inputs(
    cohort: GroundingRepairShadowCohort,
    gate_report: GroundingRepairGateReport,
    diagnosis: dict[str, Any],
) -> None:
    if not verify_grounding_repair_cohort_content_hash(cohort):
        raise ValueError("policy-v2 cohort content hash is invalid")
    if not verify_content_hash(gate_report):
        raise ValueError("gate report content hash is invalid")
    if cohort.policy_version != _POLICY_V2:
        raise ValueError(f"expected policy version {_POLICY_V2}")
    if gate_report.cohort_id != cohort.cohort_id:
        raise ValueError("gate report cohort_id does not match cohort")
    if gate_report.cohort_hash != cohort.content_hash:
        raise ValueError("gate report cohort_hash does not match cohort")
    if diagnosis.get("content_hash") != content_hash(diagnosis):
        raise ValueError("candidate safety diagnosis content hash is invalid")
    projected_skip_count = int(gate_report.metrics.get("projected_skip_case_count") or 0)
    if projected_skip_count != 0:
        raise ValueError("failure-cluster report requires a zero-projected-skip gate report")


def _diagnosis_summary(
    diagnosis: dict[str, Any],
    observations: list[GroundingRepairShadowObservation],
) -> dict[str, Any]:
    by_case_hash = {_case_id_hash(item.case_id): item for item in observations}
    entries = list(diagnosis.get("entries") or [])
    matched = [entry for entry in entries if entry.get("case_id_hash") in by_case_hash]
    matched_semantic = [
        entry
        for entry in matched
        if by_case_hash[str(entry["case_id_hash"])].reason == _SEMANTIC_REASON
    ]
    findings = [
        finding
        for entry in matched_semantic
        for finding in list(entry.get("findings") or [])
    ]
    semantic_case_count = sum(item.reason == _SEMANTIC_REASON for item in observations)
    return {
        "selected_case_count": int(diagnosis.get("selected_case_count") or len(entries)),
        "completed_case_count": int(diagnosis.get("completed_case_count") or len(entries)),
        "matched_policy_v2_case_count": len(matched),
        "matched_semantic_cluster_case_count": len(matched_semantic),
        "semantic_cluster_case_coverage": _share(len(matched_semantic), semantic_case_count),
        "finding_count": len(findings),
        "issue_counts": _counts(
            finding.get("issue_code") for finding in findings if finding.get("issue_code")
        ),
        "root_cause_counts": _counts(
            finding.get("root_cause_code")
            for finding in findings
            if finding.get("root_cause_code")
        ),
        "interpretation": (
            "The diagnosis is a reviewed subset joined by case hash. Counts describe the "
            "matched subset only and must not be extrapolated to every semantic-block case."
        ),
    }


def _policy_v3_options(
    semantic_cases: list[GroundingRepairShadowObservation],
    too_many_cases: list[GroundingRepairShadowObservation],
    retention_cases: list[GroundingRepairShadowObservation],
) -> list[dict[str, Any]]:
    repeated_uncited_only = sum(
        bool(item.initial_error_codes)
        and set(item.initial_error_codes) == {"uncited_factual_line"}
        for item in too_many_cases
    )
    return [
        {
            "option_id": "typed_semantic_claim_verification",
            "target_cluster": "semantic_support_block",
            "design_hypothesis": (
                "Replace the single lexical-support decision with typed checks for uncited "
                "claims, cited semantic mismatch, numeric mismatch, scope aggregation, and "
                "recommendation-versus-fact language."
            ),
            "observed_target_case_count": len(semantic_cases),
            "projected_skip_case_count": None,
            "expected_benefit": (
                "May identify a strictly verified subset without weakening citation, numeric, "
                "scope, or as-of safeguards. Current metadata cannot estimate that subset."
            ),
            "primary_risks": [
                "semantic_false_positive",
                "scope_aggregation_false_positive",
                "numeric_support_false_positive",
                "recommendation_fact_confusion",
                "higher_shadow_compute_latency",
            ],
            "required_evidence": [
                "offline_replay_with_no_serving_change",
                "completed_review_for_every_newly_eligible_case",
                "zero_new_safety_issue_codes_in_newly_eligible_cases",
                "shadow_compute_p95_below_5ms",
                "organic_real_user_shadow_evidence",
            ],
        },
        {
            "option_id": "signature_aware_initial_error_budget",
            "target_cluster": "too_many_initial_errors",
            "design_hypothesis": (
                "Evaluate a narrow repeated-uncited-factual-line signature separately instead "
                "of relaxing the raw initial-error-count limit for all signatures."
            ),
            "observed_target_case_count": len(too_many_cases),
            "metadata_only_upper_bound_case_count": repeated_uncited_only,
            "projected_skip_case_count": None,
            "upper_bound_is_not_eligibility_projection": True,
            "expected_benefit": (
                "Could allow semantic evaluation to run on a bounded homogeneous subset; it "
                "does not show that any of those cases are safe to skip repair."
            ),
            "primary_risks": [
                "multiple_omissions_compound_answer_quality_loss",
                "semantic_risk_currently_unmeasured_for_early_blocked_cases",
                "retention_degradation",
                "unknown_error_signature_leakage",
            ],
            "required_evidence": [
                "exclude_any_non_uncited_error_code",
                "run_full_semantic_verification_before_eligibility",
                "completed_review_for_every_newly_eligible_case",
                "noninferior_quality_and_zero_new_safety_issues",
                "organic_real_user_shadow_evidence",
            ],
        },
        {
            "option_id": "retain_retention_floor",
            "target_cluster": "retention_block",
            "design_hypothesis": (
                "Keep the current retention floor; investigate better candidate construction "
                "rather than lowering the threshold."
            ),
            "observed_target_case_count": len(retention_cases),
            "projected_skip_case_count": 0,
            "expected_benefit": (
                "Preserves the current substantive-content safeguard. The cluster is too small "
                "to justify threshold calibration."
            ),
            "primary_risks": [
                "no_repair_reduction_from_this_cluster",
                "candidate_generation_issue_remains_unresolved",
            ],
            "required_evidence": [
                "larger_reviewed_retention_boundary_sample",
                "claim_level_omission_analysis",
                "separate_approval_before_any_threshold_change",
            ],
        },
    ]


def analyze_policy_v2_failure_clusters(
    cohort: GroundingRepairShadowCohort,
    gate_report: GroundingRepairGateReport,
    diagnosis: dict[str, Any],
    *,
    generated_at: str | None = None,
) -> dict[str, Any]:
    """Build an aggregate-only policy-v2 failure-cluster artifact."""

    _validate_inputs(cohort, gate_report, diagnosis)
    qualifying = [
        item
        for item in cohort.observations
        if item.data_complete and item.origin != EvidenceOrigin.SYNTHETIC
    ]
    repair_cases = [item for item in qualifying if item.baseline_repair_attempted]
    controls = [item for item in qualifying if not item.baseline_repair_attempted]
    reason_counts = Counter(item.reason for item in repair_cases)
    unexpected_reasons = sorted(set(reason_counts) - _EXPECTED_ZERO_SKIP_REASONS)
    if unexpected_reasons:
        raise ValueError(f"unexpected policy-v2 repair blocking reasons: {unexpected_reasons}")

    semantic_cases = [item for item in repair_cases if item.reason == _SEMANTIC_REASON]
    too_many_cases = [item for item in repair_cases if item.reason == _TOO_MANY_REASON]
    retention_cases = [item for item in repair_cases if item.reason == _RETENTION_REASON]
    diagnosed = _diagnosis_summary(diagnosis, repair_cases)

    artifact: dict[str, Any] = {
        "schema_version": 1,
        "purpose": "grounding_repair_policy_v2_zero_projected_skip_failure_cluster_analysis",
        "generated_at": generated_at or _utc_now(),
        "cohort_id": cohort.cohort_id,
        "policy_version": cohort.policy_version,
        "lineage": {
            "policy_v2_cohort_content_hash": cohort.content_hash,
            "gate_report_content_hash": gate_report.content_hash,
            "candidate_safety_diagnosis_content_hash": diagnosis["content_hash"],
            "candidate_safety_source_cohort_hash": diagnosis.get("cohort_hash"),
            "candidate_safety_review_hash": diagnosis.get("review_hash"),
        },
        "scope": {
            "qualifying_observation_count": len(qualifying),
            "repair_case_count": len(repair_cases),
            "no_repair_control_count": len(controls),
            "projected_skip_case_count": 0,
            "projected_repair_call_reduction": 0.0,
            "organic_real_user_case_count": sum(
                item.origin == EvidenceOrigin.REAL for item in qualifying
            ),
            "collection_origin_counts": _counts(item.origin.value for item in qualifying),
        },
        "eligibility_waterfall": [
            {"stage": "baseline_repair_cases", "case_count": len(repair_cases)},
            {"stage": "blocked_by_initial_error_budget", "case_count": len(too_many_cases)},
            {"stage": "blocked_by_retention_floor", "case_count": len(retention_cases)},
            {"stage": "reached_semantic_analysis", "case_count": len(semantic_cases)},
            {"stage": "blocked_by_semantic_support", "case_count": len(semantic_cases)},
            {"stage": "verified_partial_pass", "case_count": 0},
        ],
        "first_blocking_reason_counts": dict(sorted(reason_counts.items())),
        "first_blocking_reason_note": (
            "Reasons are mutually exclusive first blockers. Early-blocked cases were not "
            "semantically evaluated, so cluster counts cannot be combined into skip estimates."
        ),
        "clusters": [
            {
                "cluster_code": "semantic_support_block",
                "policy_reason": _SEMANTIC_REASON,
                "metrics": _cluster_metrics(
                    semantic_cases, repair_case_count=len(repair_cases)
                ),
                "reviewed_safety_diagnosis": diagnosed,
                "interpretation": (
                    "Every case that reached semantic analysis stopped on the first observed "
                    "semantic risk. The reviewed subset contains concrete safety failures, so "
                    "removing this block without a stronger verifier is unsupported."
                ),
            },
            {
                "cluster_code": "initial_error_budget_block",
                "policy_reason": _TOO_MANY_REASON,
                "metrics": _cluster_metrics(
                    too_many_cases, repair_case_count=len(repair_cases)
                ),
                "repeated_uncited_only_case_count": sum(
                    bool(item.initial_error_codes)
                    and set(item.initial_error_codes) == {"uncited_factual_line"}
                    for item in too_many_cases
                ),
                "interpretation": (
                    "These cases stopped before semantic evaluation. A homogeneous metadata "
                    "signature is only a bounded experiment candidate, not evidence of safety."
                ),
            },
            {
                "cluster_code": "retention_block",
                "policy_reason": _RETENTION_REASON,
                "metrics": _cluster_metrics(
                    retention_cases, repair_case_count=len(repair_cases)
                ),
                "interpretation": (
                    "The observed cluster is too small to support lowering the retention floor."
                ),
            },
        ],
        "policy_v3_design_options": _policy_v3_options(
            semantic_cases, too_many_cases, retention_cases
        ),
        "decision": {
            "status": "analysis_complete_not_ready",
            "zero_projected_skip_confirmed": True,
            "keep_shadow_only": True,
            "policy_change_implemented": False,
            "serving_change_implemented": False,
            "promotion_authorized": False,
            "human_review_required_for_current_decision": False,
            "recommended_current_action": "retain_policy_v2_shadow_only",
            "blocking_taxonomy": [
                "organic_real_user_evidence_missing",
                "no_promotable_real_signature",
                "projected_repair_reduction_below_threshold",
                "completion_improvement_below_threshold",
                "policy_v3_hypotheses_not_validated",
            ],
            "next_evidence_actions": [
                "collect_organic_real_user_shadow_evidence",
                "prototype_policy_v3_options_offline_only_after_separate_approval",
                "predeclare_newly_eligible_case_safety_and_latency_gates",
                "review_every_newly_eligible_case_before_any_serving_decision",
            ],
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


__all__ = ["analyze_policy_v2_failure_clusters"]
