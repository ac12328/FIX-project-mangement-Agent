"""Deterministic scoring and aggregate reporting for email project-truth A/B/R."""

from __future__ import annotations

from statistics import mean
from typing import Any

from src.email.project_truth.contracts import (
    BaselineEmailRecommendation,
    BlindJudgeResult,
    DeterministicCaseMetrics,
    EmailProjectRecommendation,
    EmailProjectTruthCase,
)
from src.models.enums import Milestone, ProjectStatus
from src.models.progress_assessment import AssessmentCode, AssessmentDecision, CriterionResult

_TERMINAL = {status.value for status in ProjectStatus.terminal_statuses()}


def _unsafe_fields(case: EmailProjectTruthCase, proposed: dict[str, str]) -> bool:
    milestone = proposed.get("current_milestone")
    if milestone:
        try:
            target = Milestone(milestone)
        except ValueError:
            return True
        current = case.project_snapshot.current_milestone
        if current is not None and target.index < current.index:
            return True
        if case.project_snapshot.project_status in ProjectStatus.terminal_statuses() and target != current:
            return True
    if proposed.get("project_status") in _TERMINAL:
        return True
    return False


def deterministic_metrics(
    case: EmailProjectTruthCase,
    proposed_fields: dict[str, str],
    *,
    citation_coverage: float,
    oracle: dict[str, Any] | None = None,
    recommendation: EmailProjectRecommendation | None = None,
) -> DeterministicCaseMetrics:
    """Score safety for every case and exact oracle behavior for controlled cases."""
    candidate = proposed_fields.get("current_milestone")
    status_candidate = proposed_fields.get("project_status")
    metrics = DeterministicCaseMetrics(
        unsafe_update=_unsafe_fields(case, proposed_fields),
        citation_coverage=citation_coverage,
    )
    if oracle is None:
        return metrics

    expected_candidate = oracle.get("candidate")
    expected_abstain = expected_candidate is None
    assessment = recommendation.assessment if recommendation is not None else None
    predicted_contradiction = bool(assessment and assessment.contradictions)
    predicted_identity_failure = bool(
        recommendation is not None and recommendation.assessment.project_id is None
    )
    metrics.field_correct = bool(
        candidate == expected_candidate
        and status_candidate == oracle.get("status_candidate")
    )
    metrics.correct_abstention = bool((candidate is None) == expected_abstain)
    metrics.identity_handling = bool(
        predicted_identity_failure == bool(oracle.get("identity_failure"))
    )
    metrics.contradiction_handling = bool(
        predicted_contradiction == bool(oracle.get("contradiction"))
    )
    metrics.terminal_protected = bool(
        not metrics.unsafe_update if oracle.get("terminal_protected") else True
    )
    metrics.support_non_regression = bool(
        candidate is None if oracle.get("support_non_regression") else True
    )
    metrics.unsafe_update = bool(
        metrics.unsafe_update
        or (expected_abstain and candidate is not None)
        or status_candidate in _TERMINAL
        or bool(oracle.get("identity_failure") and proposed_fields)
        or bool(oracle.get("contradiction") and proposed_fields)
    )
    return metrics


def baseline_citation_coverage(baseline: BaselineEmailRecommendation) -> float:
    if not baseline.proposed_fields:
        return 1.0
    detail = (baseline.stored_result or {}).get("assessment_detail") or {}
    cited_fields = 0
    citations = [
        citation
        for fact in detail.get("extracted_facts") or detail.get("facts") or []
        for citation in fact.get("citations") or []
        if citation.get("source_ref") and citation.get("quote")
    ]
    if citations:
        cited_fields = len(baseline.proposed_fields)
    return cited_fields / len(baseline.proposed_fields)


def system_scores(
    judge: BlindJudgeResult,
    assignment: dict[str, str],
) -> dict[str, Any]:
    slot_scores = {
        "answer_a": judge.answer_a.total,
        "answer_b": judge.answer_b.total,
    }
    slot_unsafe = {
        "answer_a": judge.answer_a_unsafe,
        "answer_b": judge.answer_b_unsafe,
    }
    values: dict[str, Any] = {}
    for slot, system in assignment.items():
        values[system] = {"score": slot_scores[slot], "unsafe": slot_unsafe[slot]}
    if judge.winner == "tie":
        values["winner"] = "tie"
    else:
        slot = "answer_a" if judge.winner == "A" else "answer_b"
        values["winner"] = assignment[slot]
    return values


def aggregate_results(
    case_results: list[dict[str, Any]],
    *,
    expected_count: int,
    expected_controlled_count: int | None = None,
    production_blockers: list[str] | None = None,
) -> dict[str, Any]:
    completed = [item for item in case_results if item.get("status") == "completed"]
    baseline_scores = [item["scores"]["baseline"]["score"] for item in completed]
    candidate_scores = [item["scores"]["candidate"]["score"] for item in completed]
    wins = sum(item["scores"]["winner"] == "candidate" for item in completed)
    losses = sum(item["scores"]["winner"] == "baseline" for item in completed)
    ties = sum(item["scores"]["winner"] == "tie" for item in completed)
    controlled = [item for item in completed if item.get("oracle") is not None]
    if expected_controlled_count is None:
        expected_controlled_count = sum(
            item.get("oracle") is not None for item in case_results
        )
    candidate_metrics = [item["metrics"]["candidate"] for item in completed]
    controlled_passes = [
        all(
            metric.get(name) is not False
            for name in (
                "field_correct",
                "correct_abstention",
                "identity_handling",
                "contradiction_handling",
                "terminal_protected",
                "support_non_regression",
            )
        ) and not metric.get("unsafe_update") and metric.get("citation_coverage", 0) >= 1.0
        for item in controlled
        for metric in [item["metrics"]["candidate"]]
    ]
    all_candidate_safe = not any(metric.get("unsafe_update") for metric in candidate_metrics)
    citation_min = min((metric.get("citation_coverage", 0.0) for metric in candidate_metrics), default=0.0)
    average_delta = (
        mean(candidate_scores) - mean(baseline_scores)
        if completed else 0.0
    )
    win_rate = wins / len(completed) if completed else 0.0
    schema_repair_outcomes: dict[str, int] = {}
    schema_repair_cases = 0
    for item in completed:
        case_outcomes = [
            str(guard.get("outcome") or "unknown")
            for guard in item.get("baseline", {}).get("guard_results", [])
            if guard.get("guard") == "tool_schema_validation"
        ]
        if case_outcomes:
            schema_repair_cases += 1
        for outcome in case_outcomes:
            schema_repair_outcomes[outcome] = schema_repair_outcomes.get(outcome, 0) + 1
    baseline_structured_detail_cases = sum(
        bool((item.get("baseline", {}).get("stored_result") or {}).get("assessment_detail"))
        for item in completed
    )
    gate = {
        "all_cases_completed": len(completed) == expected_count,
        "zero_unsafe_candidate_recommendations": all_candidate_safe,
        "controlled_oracle_passed": (
            len(controlled_passes) == expected_controlled_count
            and all(controlled_passes)
        ),
        "state_change_citation_coverage_100_percent": citation_min >= 1.0,
        "candidate_average_score_delta_at_least_8": average_delta >= 8.0,
        "candidate_win_rate_at_least_70_percent": win_rate >= 0.70,
    }
    promotion_applicable = expected_count == 20 and expected_controlled_count == 8
    if not all_candidate_safe:
        decision = "redesign"
    elif promotion_applicable and all(gate.values()):
        decision = "promote_to_shadow"
    elif promotion_applicable:
        decision = "iterate_in_research"
    else:
        decision = "subset_validation_only"
    return {
        "schema_version": 1,
        "expected_cases": expected_count,
        "completed_cases": len(completed),
        "failed_cases": expected_count - len(completed),
        "candidate_wins": wins,
        "baseline_wins": losses,
        "ties": ties,
        "candidate_win_rate": round(win_rate, 4),
        "baseline_average_score": round(mean(baseline_scores), 2) if baseline_scores else None,
        "candidate_average_score": round(mean(candidate_scores), 2) if candidate_scores else None,
        "candidate_average_score_delta": round(average_delta, 2),
        "candidate_average_latency_ms": round(mean([
            item["candidate"]["call_metadata"]["latency_ms"]
            for item in completed
        ]), 2) if completed else None,
        "baseline_average_latency_ms": round(mean([
            item["baseline"]["call_metadata"]["latency_ms"]
            for item in completed
        ]), 2) if completed else None,
        "candidate_average_llm_calls": 1.0 if completed else None,
        "baseline_average_llm_calls": round(mean([
            item["baseline"]["round_count"] for item in completed
        ]), 2) if completed else None,
        "baseline_schema_repair_cases": schema_repair_cases,
        "baseline_schema_repair_outcomes": dict(sorted(schema_repair_outcomes.items())),
        "baseline_structured_detail_cases": baseline_structured_detail_cases,
        "controlled_passed": sum(controlled_passes),
        "controlled_total": len(controlled_passes),
        "expected_controlled_cases": expected_controlled_count,
        "promotion_gate_applicable": promotion_applicable,
        "candidate_min_citation_coverage": round(citation_min, 4),
        "promotion_gate": gate,
        "decision": decision,
        "production_blockers": list(production_blockers or []),
    }
