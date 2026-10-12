"""Deterministic rule training and metrics for the Policy-v3 selector experiment."""

from __future__ import annotations

import hashlib
import itertools
import json
import statistics
import time
from collections import Counter
from dataclasses import dataclass
from typing import Any, Iterable

_SPLIT_SEED = "grounding-repair-policy-v3-request-hash-split-20260810"
_TRAIN_THRESHOLD = 0.70
_MIN_TRAIN_POSITIVES_PER_CLAUSE = 3
_MAX_RULE_CLAUSES = 2

@dataclass(frozen=True, slots=True)
class _FeatureRow:
    case_id: str
    request_hash: str
    cluster_code: str
    strict_oracle_label: bool
    candidate_issue_count: int
    candidate_score: float
    baseline_score: float
    completion_savings_ms: float
    feature_compute_ms: float
    features: dict[str, Any]

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

def _split_assignment(request_hash: str) -> str:
    digest = hashlib.sha256(f"{_SPLIT_SEED}:{request_hash}".encode("utf-8")).digest()
    fraction = int.from_bytes(digest[:8], "big") / float(2**64)
    return "train" if fraction < _TRAIN_THRESHOLD else "holdout"


def _predicate(feature: str, operator: str, value: Any) -> dict[str, Any]:
    return {"feature": feature, "operator": operator, "value": value}


def _predicate_catalog() -> list[dict[str, Any]]:
    predicates: list[dict[str, Any]] = []
    for value in ("uncited_only", "unknown_citation_only", "internal_marker_only", "mixed"):
        predicates.append(_predicate("initial_error_signature_code", "eq", value))
    for feature in (
        "all_valid_cited_lines_preserved",
        "has_uncited_semantic_risk",
        "has_low_support_risk",
        "has_numeric_mismatch_risk",
        "has_scope_aggregation_risk",
    ):
        predicates.extend([
            _predicate(feature, "eq", False),
            _predicate(feature, "eq", True),
        ])
    threshold_catalog: dict[str, tuple[str, tuple[float | int, ...]]] = {
        "initial_error_count": ("le", (1, 2, 3, 4, 5)),
        "initial_error_unique_count": ("le", (1, 2, 3)),
        "candidate_error_count": ("le", (0, 1, 2)),
        "substantive_line_retention": ("ge", (0.75, 0.80, 0.85, 0.90, 0.95, 1.0)),
        "semantic_risk_code_count": ("le", (1, 2, 3, 4)),
        "source_record_count": ("ge", (1, 2, 3, 5, 8)),
        "candidate_citation_id_count": ("ge", (1, 2, 3, 5)),
        "semantic_line_count": ("le", (3, 5, 8, 12, 20)),
        "cited_semantic_line_count": ("ge", (1, 2, 3, 5, 8)),
        "uncited_semantic_line_count": ("le", (0, 1, 2, 3, 5)),
        "unknown_citation_line_count": ("le", (0, 1)),
        "semantic_risky_line_count": ("le", (1, 2, 3, 5, 8, 12)),
        "semantic_risky_line_ratio": ("le", (0.25, 0.50, 0.75, 1.0)),
        "low_support_line_count": ("le", (0, 1, 2, 3, 5, 8)),
        "numeric_mismatch_line_count": ("le", (0, 1, 2, 3, 5)),
        "scope_aggregation_risk_line_count": ("le", (0, 1, 2, 3)),
        "lexical_support_observation_count": ("ge", (1, 2, 3, 5, 8)),
        "lexical_support_min": ("ge", (0.05, 0.10, 0.20, 0.30, 0.40, 0.50)),
        "lexical_support_mean": ("ge", (0.10, 0.20, 0.30, 0.40, 0.50, 0.60)),
    }
    for feature, (operator, thresholds) in threshold_catalog.items():
        predicates.extend(_predicate(feature, operator, value) for value in thresholds)
    return predicates


def _predicate_matches(features: dict[str, Any], predicate: dict[str, Any]) -> bool:
    actual = features[predicate["feature"]]
    operator = predicate["operator"]
    expected = predicate["value"]
    if operator == "eq":
        return actual == expected
    if operator == "le":
        return float(actual) <= float(expected)
    if operator == "ge":
        return float(actual) >= float(expected)
    raise ValueError(f"unsupported predicate operator: {operator}")


def _clause_matches(features: dict[str, Any], clause: list[dict[str, Any]]) -> bool:
    return all(_predicate_matches(features, predicate) for predicate in clause)


def _rule_matches(features: dict[str, Any], rule: list[list[dict[str, Any]]]) -> bool:
    return any(_clause_matches(features, clause) for clause in rule)


def _rule_key(rule: list[list[dict[str, Any]]]) -> tuple[int, str]:
    return (
        sum(len(clause) for clause in rule),
        json.dumps(rule, sort_keys=True, separators=(",", ":")),
    )


def _train_rule(train_rows: list[_FeatureRow]) -> tuple[list[list[dict[str, Any]]], dict[str, Any]]:
    started_ns = time.perf_counter_ns()
    positive_mask = sum(
        (1 << index) for index, row in enumerate(train_rows) if row.strict_oracle_label
    )
    all_mask = (1 << len(train_rows)) - 1
    negative_mask = all_mask ^ positive_mask
    atomic_by_mask: dict[int, dict[str, Any]] = {}
    for predicate in _predicate_catalog():
        mask = sum(
            (1 << index)
            for index, row in enumerate(train_rows)
            if _predicate_matches(row.features, predicate)
        )
        if not mask:
            continue
        existing = atomic_by_mask.get(mask)
        if existing is None or json.dumps(predicate, sort_keys=True) < json.dumps(existing, sort_keys=True):
            atomic_by_mask[mask] = predicate

    atomics = sorted(atomic_by_mask.items(), key=lambda item: json.dumps(item[1], sort_keys=True))
    clauses_by_mask: dict[int, list[dict[str, Any]]] = {}
    for mask, predicate in atomics:
        if not (mask & negative_mask) and (mask & positive_mask).bit_count() >= _MIN_TRAIN_POSITIVES_PER_CLAUSE:
            clauses_by_mask[mask] = [predicate]
    for (left_mask, left), (right_mask, right) in itertools.combinations(atomics, 2):
        mask = left_mask & right_mask
        if not mask or mask & negative_mask:
            continue
        if (mask & positive_mask).bit_count() < _MIN_TRAIN_POSITIVES_PER_CLAUSE:
            continue
        clause = sorted([left, right], key=lambda item: json.dumps(item, sort_keys=True))
        existing = clauses_by_mask.get(mask)
        if existing is None or _rule_key([clause]) < _rule_key([existing]):
            clauses_by_mask[mask] = clause

    candidates = sorted(
        clauses_by_mask.items(), key=lambda item: json.dumps(item[1], sort_keys=True)
    )
    best_rule: list[list[dict[str, Any]]] = []
    best_mask = 0
    for mask, clause in candidates:
        rule = [clause]
        score = ((mask & positive_mask).bit_count(), -_rule_key(rule)[0], _rule_key(rule)[1])
        best_score = ((best_mask & positive_mask).bit_count(), -_rule_key(best_rule)[0], _rule_key(best_rule)[1])
        if score > best_score:
            best_rule, best_mask = rule, mask
    if _MAX_RULE_CLAUSES >= 2:
        for (left_mask, left), (right_mask, right) in itertools.combinations(candidates, 2):
            union_mask = left_mask | right_mask
            rule = sorted([left, right], key=lambda item: json.dumps(item, sort_keys=True))
            score = (
                (union_mask & positive_mask).bit_count(),
                -_rule_key(rule)[0],
                _rule_key(rule)[1],
            )
            best_score = (
                (best_mask & positive_mask).bit_count(),
                -_rule_key(best_rule)[0],
                _rule_key(best_rule)[1],
            )
            if score > best_score:
                best_rule, best_mask = rule, union_mask
    search_ms = (time.perf_counter_ns() - started_ns) / 1_000_000
    return best_rule, {
        "atomic_predicate_count": len(_predicate_catalog()),
        "distinct_train_atomic_mask_count": len(atomics),
        "zero_false_positive_clause_count": len(candidates),
        "search_compute_ms": round(search_ms, 6),
    }


def _segment_metrics(rows: list[_FeatureRow], selected: list[bool]) -> dict[str, Any]:
    true_positive = sum(flag and row.strict_oracle_label for row, flag in zip(rows, selected))
    false_positive = sum(flag and not row.strict_oracle_label for row, flag in zip(rows, selected))
    false_negative = sum(not flag and row.strict_oracle_label for row, flag in zip(rows, selected))
    true_negative = sum(not flag and not row.strict_oracle_label for row, flag in zip(rows, selected))
    selected_rows = [row for row, flag in zip(rows, selected) if flag]
    return {
        "case_count": len(rows),
        "strict_oracle_positive_count": sum(row.strict_oracle_label for row in rows),
        "selected_case_count": len(selected_rows),
        "true_positive_count": true_positive,
        "false_positive_count": false_positive,
        "false_negative_count": false_negative,
        "true_negative_count": true_negative,
        "precision": _share(true_positive, true_positive + false_positive),
        "recall": _share(true_positive, true_positive + false_negative),
        "selected_candidate_issue_count": sum(
            row.candidate_issue_count for row in selected_rows
        ),
        "selected_cluster_counts": _counts(row.cluster_code for row in selected_rows),
        "selected_candidate_minus_baseline_score": _numeric_summary(
            row.candidate_score - row.baseline_score for row in selected_rows
        ),
        "selected_theoretical_completion_savings_ms": _numeric_summary(
            row.completion_savings_ms for row in selected_rows
        ),
    }


def _reproducibility_projection(artifact: dict[str, Any]) -> dict[str, Any]:
    projected = json.loads(json.dumps(artifact))
    projected.pop("content_hash", None)
    projected.pop("generated_at", None)
    projected.get("training", {}).pop("search_compute_ms", None)
    projected.pop("performance", None)
    return projected

__all__ = [
    "_FeatureRow",
    "_MAX_RULE_CLAUSES",
    "_MIN_TRAIN_POSITIVES_PER_CLAUSE",
    "_SPLIT_SEED",
    "_TRAIN_THRESHOLD",
    "_numeric_summary",
    "_predicate_catalog",
    "_reproducibility_projection",
    "_rule_matches",
    "_segment_metrics",
    "_share",
    "_split_assignment",
    "_train_rule",
]
