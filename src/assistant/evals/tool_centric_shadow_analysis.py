"""Pure aggregation helpers for tool-centric shadow router evidence."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Iterable

from src.assistant.evals.tool_centric_query_v2_analysis import metric, ratio


def aggregate(
    progress: dict[str, Any],
    selected: Iterable[Any],
    *,
    experiment_id: str,
    worker_evidence: dict[str, Any] | None = None,
) -> dict[str, Any]:
    selected_cases = list(selected)
    selected_ids = {case.case_id for case in selected_cases}
    rows = [
        item
        for case_id, item in progress.get("cases", {}).items()
        if case_id in selected_ids and item.get("status") == "complete"
    ]

    expected_typed = [item for item in rows if item["expected_route"] == "portfolio_typed_read"]
    expected_fallback = [item for item in rows if item["expected_route"] == "fallback"]
    routed_typed = [item for item in rows if item.get("observation", {}).get("route") == "portfolio_typed_read"]
    tp = sum(item.get("observation", {}).get("route") == "portfolio_typed_read" for item in expected_typed)
    fn = len(expected_typed) - tp
    fp = sum(item.get("observation", {}).get("route") == "portfolio_typed_read" for item in expected_fallback)
    tn = len(expected_fallback) - fp
    completed_typed = [
        item for item in expected_typed
        if item.get("observation", {}).get("execution_status") == "completed"
    ]
    injected_failures = [item for item in rows if item.get("fault") not in (None, "none")]
    reviews = [item["review"] for item in rows if (item.get("review") or {}).get("complete")]
    baselines = [item["baseline"] for item in rows if not item.get("baseline", {}).get("skipped")]

    summary = {
        "case_count": len(rows),
        "expected_typed_count": len(expected_typed),
        "expected_fallback_count": len(expected_fallback),
        "true_positive": tp,
        "false_positive": fp,
        "false_negative": fn,
        "true_negative": tn,
        "route_precision": ratio(tp, tp + fp),
        "route_recall": ratio(tp, tp + fn),
        "fallback_safety": ratio(tn, len(expected_fallback)),
        "route_correct_count": sum(bool(item.get("route_correct")) for item in rows),
        "reason_accepted_count": sum(bool(item.get("reason_accepted")) for item in rows),
        "typed_completed_count": len(completed_typed),
        "typed_execution_exact_count": sum(bool(item.get("execution_exact")) for item in completed_typed),
        "typed_execution_exact_rate": ratio(sum(bool(item.get("execution_exact")) for item in completed_typed), len(completed_typed)),
        "served_answer_change_count": sum(bool(item.get("served_answer_changed")) for item in rows),
        "scope_widening_count": sum(bool(item.get("scope_widened")) for item in rows),
        "shadow_business_write_count": sum(int(item.get("shadow_business_writes") or 0) for item in rows),
        "candidate_latency_ms": metric(item.get("observation", {}).get("elapsed_ms", 0) for item in routed_typed),
        "planner_latency_ms": metric(item.get("observation", {}).get("planner_ms", 0) for item in rows if item.get("observation")),
        "planner_input_tokens": metric(item.get("observation", {}).get("estimated_input_tokens", 0) for item in rows if item.get("observation", {}).get("llm_calls")),
        "typed_payload_bytes": metric(item.get("candidate_result_bytes", 0) for item in completed_typed),
        "baseline_latency_seconds": metric(item.get("elapsed_seconds", 0) for item in baselines),
        "failure_case_count": len(injected_failures),
        "failure_isolated_count": sum(bool(item.get("failure_isolated")) for item in injected_failures),
        "review_count": len(reviews),
        "candidate_score": metric(review["candidate_score"] for review in reviews if review.get("candidate_score") is not None),
        "baseline_score": metric(review["baseline_score"] for review in reviews if review.get("baseline_score") is not None),
        "candidate_preferred": sum(review.get("preferred_answer") == "candidate" for review in reviews),
        "baseline_preferred": sum(review.get("preferred_answer") == "baseline" for review in reviews),
        "ties": sum(review.get("preferred_answer") == "tie" for review in reviews),
        "candidate_issue_count": sum(len(review.get("candidate_issue_codes") or []) for review in reviews),
        "baseline_issue_count": sum(len(review.get("baseline_issue_codes") or []) for review in reviews),
    }
    gates = {
        "route_precision_1": summary["route_precision"] == 1.0,
        "route_recall_1": summary["route_recall"] == 1.0,
        "fallback_safety_1": summary["fallback_safety"] == 1.0,
        "typed_execution_exact_1": summary["typed_execution_exact_rate"] == 1.0,
        "served_answer_changes_0": summary["served_answer_change_count"] == 0,
        "scope_widening_0": summary["scope_widening_count"] == 0,
        "shadow_business_writes_0": summary["shadow_business_write_count"] == 0,
        "failure_isolation_1": summary["failure_isolated_count"] == summary["failure_case_count"],
    }
    if worker_evidence:
        gates["enqueue_p95_lt_5ms"] = float(worker_evidence.get("enqueue_latency_ms", {}).get("p95") or 999999) < 5.0
        gates["queue_saturation_fail_open"] = bool(worker_evidence.get("queue_saturation", {}).get("fail_open"))
        gates["worker_survives_observer_error"] = bool(worker_evidence.get("observer_recovery", {}).get("recovered"))
        gates["multi_turn_scope_isolation"] = bool(worker_evidence.get("multi_turn", {}).get("scope_isolated"))

    def finding(item: dict[str, Any]) -> dict[str, Any]:
        return {
            "case_id": item["case_id"],
            "expected_route": item["expected_route"],
            "actual_route": item.get("observation", {}).get("route"),
            "reason_code": item.get("observation", {}).get("reason_code"),
            "execution_status": item.get("observation", {}).get("execution_status"),
            "route_correct": item.get("route_correct"),
            "reason_accepted": item.get("reason_accepted"),
            "execution_exact": item.get("execution_exact"),
            "scope_widened": item.get("scope_widened"),
            "error": item.get("error"),
            "candidate_issues": (item.get("review") or {}).get("candidate_issue_codes") or [],
        }

    safety_failures = [
        finding(item)
        for item in rows
        if not (
            item.get("route_correct")
            and not item.get("served_answer_changed")
            and not item.get("scope_widened")
            and not item.get("shadow_business_writes")
            and (item.get("execution_exact") is not False)
            and not (item.get("review") or {}).get("candidate_issue_codes")
        )
    ]
    classification_findings = [
        finding(item) for item in rows if not item.get("reason_accepted")
    ]

    return {
        "schema_version": 1,
        "experiment_id": experiment_id,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "research_only": True,
        "production_routing_changed": False,
        "summary": summary,
        "worker_evidence": worker_evidence or {},
        "gates": gates,
        "all_gates_passed": all(gates.values()),
        "failed_cases": safety_failures,
        "classification_findings": classification_findings,
    }


__all__ = ["aggregate"]
