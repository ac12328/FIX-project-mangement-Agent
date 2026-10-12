"""Offline hash-split selector experiment for grounding-repair Policy-v3 research."""

from __future__ import annotations

import hashlib
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from src.assistant import grounding_repair_shadow as shadow
from src.assistant.evals.artifacts import content_hash
from src.assistant.evals.grounding_repair import (
    GroundingRepairReviewBatch,
    GroundingRepairShadowCohort,
    ReviewStatus,
)
from src.assistant.evals.grounding_repair_policy_replay import (
    _answer_hash,
    _decrypt_payload,
    _event_rows,
)
from src.assistant.evals.grounding_review_models import validate_review_lineage
from src.assistant.evals.grounding_repair_selector_rules import (
    _FeatureRow,
    _MAX_RULE_CLAUSES,
    _MIN_TRAIN_POSITIVES_PER_CLAUSE,
    _SPLIT_SEED,
    _TRAIN_THRESHOLD,
    _numeric_summary,
    _predicate_catalog,
    _reproducibility_projection,
    _rule_matches,
    _segment_metrics,
    _share,
    _split_assignment,
    _train_rule,
)

_EXPERIMENT_VERSION = "grounding-repair-policy-v3-hash-split-selector-v1"
_REASON_TO_CLUSTER = {
    "candidate_semantic_support_unverified": "semantic_support_block",
    "too_many_initial_errors": "initial_error_budget_block",
    "substantive_retention_below_threshold": "retention_block",
}




def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")




def _initial_signature(initial_error_codes: list[str]) -> str:
    codes = set(initial_error_codes)
    if not codes:
        return "none"
    if codes == {"uncited_factual_line"}:
        return "uncited_only"
    if codes == {"unknown_citation"}:
        return "unknown_citation_only"
    if codes == {"internal_tool_marker"}:
        return "internal_marker_only"
    return "mixed"


def _semantic_line_features(
    candidate_answer: str,
    sources: list[dict[str, Any]],
) -> dict[str, Any]:
    source_records = {
        str(source.get("source_id") or "").strip(): source
        for source in sources
        if str(source.get("source_id") or "").strip()
    }
    source_support = shadow.build_grounding_semantic_source_support(sources)
    semantic_line_count = 0
    cited_semantic_line_count = 0
    uncited_semantic_line_count = 0
    unknown_citation_line_count = 0
    low_support_line_count = 0
    numeric_mismatch_line_count = 0
    scope_aggregation_risk_line_count = 0
    risky_line_count = 0
    citation_ids: set[str] = set()
    support_ratios: list[float] = []

    for raw_line in str(candidate_answer or "").splitlines():
        if shadow._line_is_structural(raw_line):
            continue
        semantic_line_count += 1
        citations = set(shadow._CITATION_RE.findall(raw_line))
        citation_ids.update(citations)
        claim_text = shadow._CITATION_RE.sub("", raw_line) if citations else raw_line
        line_risky = False
        if not citations:
            uncited_semantic_line_count += 1
            line_risky = True
        elif not citations.issubset(source_records):
            unknown_citation_line_count += 1
            line_risky = True
        else:
            cited_semantic_line_count += 1
            supports = [source_support[source_id] for source_id in citations]
            claim_tokens = shadow._semantic_tokens(claim_text)
            evidence_tokens = set().union(*(item.tokens for item in supports))
            evidence_numbers = set().union(*(item.numbers for item in supports))
            source_kinds = {item.kind for item in supports}
            support_ratio = (
                len(claim_tokens & evidence_tokens) / len(claim_tokens)
                if claim_tokens
                else 1.0
            )
            support_ratios.append(support_ratio)
            if support_ratio < shadow.GROUNDING_REPAIR_MIN_CITATION_TOKEN_SUPPORT:
                low_support_line_count += 1
                line_risky = True
            claim_numbers = set(shadow._NUMBER_RE.findall(claim_text))
            if not claim_numbers.issubset(evidence_numbers):
                numeric_mismatch_line_count += 1
                line_risky = True
            if (
                len(citations) == 1
                and source_kinds & {"portfolio", "portfolio_query"}
                and shadow._SCOPE_AGGREGATION_RE.search(claim_text)
            ):
                scope_aggregation_risk_line_count += 1
                line_risky = True
        risky_line_count += int(line_risky)

    support_summary = _numeric_summary(support_ratios)
    return {
        "source_record_count": len(source_records),
        "candidate_citation_id_count": len(citation_ids),
        "semantic_line_count": semantic_line_count,
        "cited_semantic_line_count": cited_semantic_line_count,
        "uncited_semantic_line_count": uncited_semantic_line_count,
        "unknown_citation_line_count": unknown_citation_line_count,
        "semantic_risky_line_count": risky_line_count,
        "semantic_risky_line_ratio": (
            risky_line_count / semantic_line_count if semantic_line_count else 0.0
        ),
        "low_support_line_count": low_support_line_count,
        "numeric_mismatch_line_count": numeric_mismatch_line_count,
        "scope_aggregation_risk_line_count": scope_aggregation_risk_line_count,
        "lexical_support_observation_count": int(support_summary["count"] or 0),
        "lexical_support_min": float(support_summary["min"] or 0.0),
        "lexical_support_mean": float(support_summary["mean"] or 0.0),
    }


def _extract_feature_row(observation: Any, review: Any, payload: dict[str, Any]) -> _FeatureRow:
    started_ns = time.perf_counter_ns()
    prepared_answer = str(payload.get("prepared_answer") or "")
    candidate_answer = str(payload.get("candidate_answer") or "")
    sources = [item for item in payload.get("sources") or [] if isinstance(item, dict)]
    tool_results = [
        item for item in payload.get("tool_results") or [] if isinstance(item, dict)
    ]
    source_ids = {
        str(item.get("source_id") or "").strip()
        for item in sources
        if str(item.get("source_id") or "").strip()
    }
    factual_tools_used = any(item.get("tool") != "think" for item in tool_results)
    decision = shadow.evaluate_grounding_repair_shadow(
        prepared_answer=prepared_answer,
        initial_error_codes=observation.initial_error_codes,
        candidate_answer=candidate_answer,
        candidate_error_codes=observation.candidate_error_codes,
        source_ids=source_ids,
        factual_tools_used=factual_tools_used,
        sources=sources,
    )
    semantic = _semantic_line_features(candidate_answer, sources)
    risk_codes = set(decision.semantic_risk_codes)
    features = {
        "initial_error_count": len(observation.initial_error_codes),
        "initial_error_unique_count": len(set(observation.initial_error_codes)),
        "initial_error_signature_code": _initial_signature(observation.initial_error_codes),
        "candidate_error_count": len(observation.candidate_error_codes),
        "substantive_line_retention": float(decision.substantive_line_retention),
        "all_valid_cited_lines_preserved": bool(decision.all_valid_cited_lines_preserved),
        "semantic_risk_code_count": len(risk_codes),
        "has_uncited_semantic_risk": "uncited_semantic_content" in risk_codes,
        "has_low_support_risk": "citation_semantic_support_low" in risk_codes,
        "has_numeric_mismatch_risk": "citation_numeric_support_mismatch" in risk_codes,
        "has_scope_aggregation_risk": "portfolio_scope_aggregation_unverified" in risk_codes,
        **semantic,
    }
    compute_ms = (time.perf_counter_ns() - started_ns) / 1_000_000
    return _FeatureRow(
        case_id=observation.case_id,
        request_hash=str(observation.request_hash or ""),
        cluster_code=_REASON_TO_CLUSTER[observation.reason],
        strict_oracle_label=(review.candidate_pass is True and not review.candidate_issue_codes),
        candidate_issue_count=len(review.candidate_issue_codes),
        candidate_score=float(review.candidate_score),
        baseline_score=float(review.baseline_score),
        completion_savings_ms=max(0.0, observation.baseline_ready_ms - observation.candidate_ready_ms),
        feature_compute_ms=compute_ms,
        features=features,
    )




def analyze_policy_v3_hash_split_selector(
    database_path: Path,
    cohort: GroundingRepairShadowCohort,
    reviews: GroundingRepairReviewBatch,
    oracle_report: dict[str, Any],
    *,
    generated_at: str | None = None,
) -> dict[str, Any]:
    """Train once on request-hash groups and evaluate the locked rule once on holdout."""

    validate_review_lineage(cohort, reviews)
    if any(item.status != ReviewStatus.REVIEWED for item in reviews.reviews):
        raise ValueError("selector experiment requires completed repair reviews")
    if oracle_report.get("content_hash") != content_hash(oracle_report):
        raise ValueError("oracle report content hash is invalid")
    if oracle_report.get("cohort_id") != cohort.cohort_id:
        raise ValueError("oracle report cohort_id does not match cohort")
    if oracle_report.get("lineage", {}).get("review_hash") != reviews.content_hash:
        raise ValueError("oracle report review_hash does not match reviews")
    if oracle_report.get("analysis_class") != "retrospective_offline_outcome_oracle":
        raise ValueError("selector experiment requires the retrospective oracle report")

    repair_observations = {
        item.case_id: item
        for item in cohort.observations
        if item.data_complete and item.baseline_repair_attempted
    }
    unknown_reasons = sorted(
        {item.reason for item in repair_observations.values()} - set(_REASON_TO_CLUSTER)
    )
    if unknown_reasons:
        raise ValueError("repair cohort contains unsupported first-blocking reasons")
    rows_by_event = _event_rows(Path(database_path), sorted(repair_observations))
    reviews_by_case = {item.case_id: item for item in reviews.reviews}
    feature_rows: list[_FeatureRow] = []
    for case_id in sorted(repair_observations):
        observation = repair_observations[case_id]
        if not observation.request_hash:
            raise ValueError("selector split requires request_hash for every repair case")
        event_row = rows_by_event[case_id]
        if (
            observation.trace_payload_hash
            and str(event_row["payload_hash"] or "") != observation.trace_payload_hash
        ):
            raise ValueError("selector trace payload lineage mismatch")
        payload = _decrypt_payload(event_row)
        for field, expected in (
            ("prepared_answer", observation.prepared_answer_hash),
            ("candidate_answer", observation.candidate_answer_hash),
            ("baseline_answer", observation.baseline_answer_hash),
        ):
            if not expected or _answer_hash(str(payload.get(field) or "")) != expected:
                raise ValueError("selector answer lineage mismatch")
        feature_rows.append(
            _extract_feature_row(observation, reviews_by_case[case_id], payload)
        )

    train_rows = [row for row in feature_rows if _split_assignment(row.request_hash) == "train"]
    holdout_rows = [row for row in feature_rows if _split_assignment(row.request_hash) == "holdout"]
    if not train_rows or not holdout_rows:
        raise ValueError("deterministic hash split produced an empty segment")
    rule, search = _train_rule(train_rows)

    def evaluate(rows: list[_FeatureRow]) -> tuple[list[bool], list[float]]:
        selected: list[bool] = []
        inference_ms: list[float] = []
        for row in rows:
            started_ns = time.perf_counter_ns()
            selected.append(_rule_matches(row.features, rule) if rule else False)
            selector_ms = (time.perf_counter_ns() - started_ns) / 1_000_000
            inference_ms.append(row.feature_compute_ms + selector_ms)
        return selected, inference_ms

    train_selected, train_inference_ms = evaluate(train_rows)
    holdout_selected, holdout_inference_ms = evaluate(holdout_rows)
    all_rows = train_rows + holdout_rows
    all_selected = train_selected + holdout_selected
    all_inference_ms = train_inference_ms + holdout_inference_ms
    train_metrics = _segment_metrics(train_rows, train_selected)
    holdout_metrics = _segment_metrics(holdout_rows, holdout_selected)
    combined_metrics = _segment_metrics(all_rows, all_selected)
    feature_compute = _numeric_summary(row.feature_compute_ms for row in all_rows)
    inference_compute = _numeric_summary(all_inference_ms)
    gates = {
        "training_selected_case_count_at_least_3": train_metrics["selected_case_count"] >= 3,
        "training_false_positive_count_zero": train_metrics["false_positive_count"] == 0,
        "holdout_selected_case_count_at_least_1": holdout_metrics["selected_case_count"] >= 1,
        "holdout_false_positive_count_zero": holdout_metrics["false_positive_count"] == 0,
        "all_selected_candidate_issue_count_zero": combined_metrics["selected_candidate_issue_count"] == 0,
        "selector_inference_p95_below_5ms": (
            inference_compute["p95"] is not None and inference_compute["p95"] < 5.0
        ),
        "plaintext_features_forbidden": True,
        "per_case_feature_rows_not_persisted": True,
        "runtime_wiring_absent": True,
        "serving_change_absent": True,
    }
    warrants_further_review = all(gates.values())
    artifact: dict[str, Any] = {
        "schema_version": 1,
        "purpose": "grounding_repair_policy_v3_hash_split_selector_experiment",
        "experiment_version": _EXPERIMENT_VERSION,
        "generated_at": generated_at or _utc_now(),
        "cohort_id": cohort.cohort_id,
        "analysis_class": "offline_controlled_hash_split_experiment",
        "lineage": {
            "cohort_hash": cohort.content_hash,
            "review_hash": reviews.content_hash,
            "oracle_report_hash": oracle_report["content_hash"],
            "source_database_hash": cohort.source_database_hash,
        },
        "split": {
            "grouping_key": "request_hash",
            "split_seed_hash": "sha256:" + hashlib.sha256(_SPLIT_SEED.encode("utf-8")).hexdigest(),
            "train_threshold": _TRAIN_THRESHOLD,
            "train_case_count": len(train_rows),
            "holdout_case_count": len(holdout_rows),
            "train_request_group_count": len({row.request_hash for row in train_rows}),
            "holdout_request_group_count": len({row.request_hash for row in holdout_rows}),
            "request_group_overlap_count": len(
                {row.request_hash for row in train_rows}
                & {row.request_hash for row in holdout_rows}
            ),
            "train_strict_oracle_positive_count": sum(
                row.strict_oracle_label for row in train_rows
            ),
            "holdout_strict_oracle_positive_count": sum(
                row.strict_oracle_label for row in holdout_rows
            ),
        },
        "feature_contract": {
            "plaintext_features_used": False,
            "feature_row_persistence": False,
            "feature_names": sorted(feature_rows[0].features),
            "feature_count": len(feature_rows[0].features),
            "feature_families": [
                "initial_error_taxonomy",
                "retention_and_citation_structure",
                "semantic_risk_taxonomy",
                "semantic_line_counts",
                "lexical_support_aggregates",
                "numeric_and_scope_risk_counts",
            ],
        },
        "training": {
            "method": "zero_train_false_positive_or_of_up_to_two_conjunctive_clauses",
            "minimum_train_positives_per_clause": _MIN_TRAIN_POSITIVES_PER_CLAUSE,
            "maximum_rule_clause_count": _MAX_RULE_CLAUSES,
            "selected_rule": rule,
            **search,
        },
        "train_metrics": train_metrics,
        "holdout_metrics": holdout_metrics,
        "combined_metrics": {
            **combined_metrics,
            "projected_repair_call_reduction": _share(
                combined_metrics["selected_case_count"], len(all_rows)
            ),
        },
        "performance": {
            "feature_compute_ms": feature_compute,
            "selector_inference_including_features_ms": inference_compute,
            "database_read_and_decrypt_excluded": True,
            "training_search_excluded_from_inference": True,
        },
        "gates": gates,
        "all_experiment_gates_passed": warrants_further_review,
        "decision": {
            "status": (
                "controlled_holdout_passed_further_offline_review_only"
                if warrants_further_review
                else "controlled_holdout_failed_retain_policy_v2_shadow_only"
            ),
            "selector_warrants_further_offline_review": warrants_further_review,
            "keep_shadow_only": True,
            "policy_change_implemented": False,
            "runtime_selector_implemented": False,
            "serving_change_implemented": False,
            "promotion_authorized": False,
            "organic_real_user_evidence_required": True,
        },
        "privacy": {
            "plaintext_persisted": False,
            "per_case_records_persisted": False,
            "feature_rows_persisted": False,
            "answers_persisted": False,
            "questions_persisted": False,
            "source_content_persisted": False,
            "aggregate_metadata_only": True,
        },
    }
    artifact["content_hash"] = content_hash(artifact)
    return artifact


__all__ = [
    "analyze_policy_v3_hash_split_selector",
    "_reproducibility_projection",
]
