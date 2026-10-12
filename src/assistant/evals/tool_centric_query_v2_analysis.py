"""Pure comparison and aggregation helpers for tool-centric query simulations."""

from __future__ import annotations

import json
import math
import statistics
from datetime import datetime, timezone
from typing import Any, Iterable

from src.assistant.portfolio_query_v2 import PortfolioQueryV2Request


def json_bytes(value: Any) -> int:
    return len(
        json.dumps(value, ensure_ascii=False, separators=(",", ":"), default=str).encode("utf-8")
    )


def planner_comparison(expected: dict[str, Any], candidate: dict[str, Any]) -> dict[str, Any]:
    actual = candidate.get("plan", {}).get("query")
    if actual is None:
        return {
            "exact": False,
            "operation": False,
            "filters": False,
            "scope_semantics": False,
            "fields": False,
            "sort": False,
            "differences": ["no_query"],
        }
    expected_normalized = PortfolioQueryV2Request.model_validate(expected).model_dump(mode="json")
    actual_normalized = PortfolioQueryV2Request.model_validate(actual).model_dump(mode="json")
    fields_equal = set(actual_normalized["fields"]) == set(expected_normalized["fields"])
    sort_equal = actual_normalized["sort"] == expected_normalized["sort"]
    differences = [
        key
        for key in ("operation", "filters", "include_terminal", "group_by")
        if actual_normalized[key] != expected_normalized[key]
    ]
    if not fields_equal:
        differences.append("fields")
    if not sort_equal:
        differences.append("sort")
    return {
        "exact": not differences,
        "operation": actual_normalized["operation"] == expected_normalized["operation"],
        "filters": actual_normalized["filters"] == expected_normalized["filters"],
        "scope_semantics": actual_normalized["include_terminal"] == expected_normalized["include_terminal"],
        "fields": fields_equal,
        "sort": sort_equal,
        "differences": differences,
    }


def result_signature(
    result: dict[str, Any] | None,
    *,
    fields: Iterable[str] | None = None,
) -> Any:
    if not result:
        return None
    operation = result.get("operation")
    if operation in {"count", "exists"}:
        return {
            "operation": operation,
            "value": result.get("value"),
            "matched_count": result.get("matched_count"),
        }
    if operation == "group":
        return {"operation": operation, "groups": result.get("groups") or []}
    rows = result.get("results") or []
    inferred_fields = tuple(
        key
        for key in (rows[0].keys() if rows else ())
        if key != "source_id"
    )
    projected_fields = tuple(fields or result.get("fields") or inferred_fields)
    normalized_rows = [
        {field: row.get(field) for field in projected_fields}
        for row in rows
    ]
    normalized_rows.sort(
        key=lambda row: tuple(str(row.get(field) or "").casefold() for field in projected_fields)
    )
    return {
        "operation": operation,
        "total": result.get("total"),
        "rows": normalized_rows,
    }


def selection_signature(result: dict[str, Any] | None) -> Any:
    """Compare selected records independently of harmless projection and ordering choices."""
    if not result or result.get("operation") != "list":
        return result_signature(result)
    return {
        "operation": "list",
        "total": result.get("total"),
        "project_ids": sorted(
            str(row.get("project_id") or "")
            for row in (result.get("results") or [])
        ),
    }


def percentile(values: Iterable[float], quantile: float) -> float | None:
    ordered = sorted(float(value) for value in values)
    if not ordered:
        return None
    position = (len(ordered) - 1) * quantile
    lower = math.floor(position)
    upper = math.ceil(position)
    if lower == upper:
        return ordered[lower]
    fraction = position - lower
    return ordered[lower] + (ordered[upper] - ordered[lower]) * fraction


def metric(values: Iterable[float]) -> dict[str, float | int | None]:
    collected = [float(value) for value in values]
    return {
        "count": len(collected),
        "mean": round(statistics.fmean(collected), 4) if collected else None,
        "p50": round(percentile(collected, 0.50), 4) if collected else None,
        "p95": round(percentile(collected, 0.95), 4) if collected else None,
    }


def ratio(numerator: int, denominator: int) -> float | None:
    return round(numerator / denominator, 6) if denominator else None


def aggregate(
    progress: dict[str, Any],
    selected: Iterable[Any],
    *,
    experiment_id: str,
) -> dict[str, Any]:
    selected_ids = {case.case_id for case in selected}
    rows = [
        item
        for case_id, item in progress.get("cases", {}).items()
        if case_id in selected_ids and item.get("status") == "complete"
    ]
    by_split = {
        split: [item for item in rows if item.get("split") == split]
        for split in ("development", "holdout")
    }

    def summary(items: list[dict[str, Any]]) -> dict[str, Any]:
        baselines = [item for item in items if not item["baseline"].get("skipped")]
        reviews = [item["review"] for item in items if (item.get("review") or {}).get("complete")]
        return {
            "case_count": len(items),
            "candidate_success_count": sum(not item["candidate"].get("error") for item in items),
            "baseline_success_count": sum(not item["baseline"].get("error") for item in baselines),
            "planner_exact_count": sum(bool(item["planner_comparison"]["exact"]) for item in items),
            "planner_operation_accuracy": ratio(sum(bool(item["planner_comparison"]["operation"]) for item in items), len(items)),
            "planner_filter_accuracy": ratio(sum(bool(item["planner_comparison"]["filters"]) for item in items), len(items)),
            "execution_exact_count": sum(bool(item["execution_exact"]) for item in items),
            "execution_exact_rate": ratio(sum(bool(item["execution_exact"]) for item in items), len(items)),
            "candidate_latency_seconds": metric(item["candidate"]["elapsed_seconds"] for item in items),
            "baseline_latency_seconds": metric(item["baseline"]["elapsed_seconds"] for item in baselines),
            "candidate_llm_calls": metric(item["candidate"].get("trace_metrics", {}).get("llm_calls", item["candidate"].get("llm_calls", 0)) for item in items),
            "baseline_llm_calls": metric(item["baseline"].get("trace_metrics", {}).get("llm_calls", 0) for item in baselines),
            "candidate_tokens_in": metric(item["candidate"].get("trace_metrics", {}).get("tokens_in", item["candidate"].get("estimated_input_tokens", 0)) for item in items),
            "baseline_tokens_in": metric(item["baseline"].get("trace_metrics", {}).get("tokens_in", 0) for item in baselines),
            "candidate_tool_result_bytes": metric(item["candidate"].get("tool_result_bytes", 0) for item in items),
            "baseline_tool_result_bytes": metric(sum(json_bytes(trace.get("result")) for trace in item["baseline"].get("tool_results", [])) for item in baselines),
            "review_count": len(reviews),
            "candidate_score": metric(review["candidate_score"] for review in reviews if review.get("candidate_score") is not None),
            "baseline_score": metric(review["baseline_score"] for review in reviews if review.get("baseline_score") is not None),
            "candidate_preferred": sum(review.get("preferred_answer") == "candidate" for review in reviews),
            "baseline_preferred": sum(review.get("preferred_answer") == "baseline" for review in reviews),
            "ties": sum(review.get("preferred_answer") == "tie" for review in reviews),
            "candidate_issue_count": sum(len(review.get("candidate_issue_codes") or []) for review in reviews),
            "baseline_issue_count": sum(len(review.get("baseline_issue_codes") or []) for review in reviews),
        }

    all_summary = summary(rows)
    baseline_p50 = all_summary["baseline_latency_seconds"]["p50"]
    candidate_p50 = all_summary["candidate_latency_seconds"]["p50"]
    all_summary["latency_p50_reduction_percent"] = (
        round((1 - candidate_p50 / baseline_p50) * 100, 3)
        if baseline_p50 and candidate_p50 is not None
        else None
    )
    baseline_tokens = all_summary["baseline_tokens_in"]["mean"]
    candidate_tokens = all_summary["candidate_tokens_in"]["mean"]
    all_summary["input_token_reduction_percent"] = (
        round((1 - candidate_tokens / baseline_tokens) * 100, 3)
        if baseline_tokens and candidate_tokens is not None
        else None
    )
    return {
        "schema_version": 1,
        "experiment_id": experiment_id,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "research_only": True,
        "production_routing_changed": False,
        "all": all_summary,
        "development": summary(by_split["development"]),
        "holdout": summary(by_split["holdout"]),
        "failed_cases": [
            {
                "case_id": item["case_id"],
                "planner_differences": item["planner_comparison"]["differences"],
                "execution_exact": item["execution_exact"],
                "candidate_error": item["candidate"].get("error"),
                "baseline_error": item["baseline"].get("error"),
                "candidate_issues": (item.get("review") or {}).get("candidate_issue_codes") or [],
                "baseline_issues": (item.get("review") or {}).get("baseline_issue_codes") or [],
            }
            for item in rows
            if (
                not item["planner_comparison"]["exact"]
                or not item["execution_exact"]
                or item["candidate"].get("error")
                or (item.get("review") or {}).get("candidate_issue_codes")
            )
        ],
    }


__all__ = [
    "aggregate",
    "json_bytes",
    "metric",
    "percentile",
    "planner_comparison",
    "ratio",
    "result_signature",
    "selection_signature",
]
