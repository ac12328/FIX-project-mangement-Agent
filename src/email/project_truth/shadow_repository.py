"""Privacy-safe persistence for Email Project Truth shadow observations."""

from __future__ import annotations

import hashlib
import json
import sqlite3
import uuid
from dataclasses import dataclass, field
from typing import Mapping


EMAIL_PROJECT_TRUTH_SHADOW_POLICY_VERSION = "email-project-truth-shadow-v1"


@dataclass(frozen=True)
class EmailProjectTruthShadowRecord:
    project_id: str
    source_ref: str
    baseline_status: str
    candidate_status: str
    baseline_fields: Mapping[str, str] = field(default_factory=dict)
    candidate_fields: Mapping[str, str] = field(default_factory=dict)
    citation_coverage: float = 0.0
    unsafe_update: bool = False
    review_required: bool = False
    review_reason_count: int = 0
    accepted_citations: int = 0
    total_citations: int = 0
    rejected_citations: int = 0
    model: str | None = None
    latency_ms: int = 0
    queue_lag_ms: float = 0.0
    parent_trace_id: str | None = None
    parent_run_id: str | None = None
    shadow_trace_id: str | None = None
    shadow_run_id: str | None = None
    error_category: str | None = None
    policy_version: str = EMAIL_PROJECT_TRUTH_SHADOW_POLICY_VERSION


def record_email_project_truth_shadow(
    conn: sqlite3.Connection,
    item: EmailProjectTruthShadowRecord,
) -> str:
    """Persist only hashes, field names, counters, and trace identities."""
    observation_id = f"epts_{uuid.uuid4().hex}"
    baseline_hashes = _field_hashes(item.baseline_fields)
    candidate_hashes = _field_hashes(item.candidate_fields)
    conn.execute(
        """INSERT INTO email_project_truth_shadow
           (observation_id,policy_version,project_id,source_ref_hash,
            parent_trace_id,parent_run_id,shadow_trace_id,shadow_run_id,
            baseline_status,candidate_status,baseline_field_hashes_json,
            candidate_field_hashes_json,comparison_classification,
            citation_coverage,unsafe_update,review_required,review_reason_count,
            accepted_citations,total_citations,rejected_citations,model,
            latency_ms,queue_lag_ms,error_category,created_at)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,datetime('now'))""",
        (
            observation_id,
            item.policy_version,
            item.project_id,
            _hash_text(item.source_ref),
            item.parent_trace_id,
            item.parent_run_id,
            item.shadow_trace_id,
            item.shadow_run_id,
            item.baseline_status,
            item.candidate_status,
            json.dumps(baseline_hashes, sort_keys=True, separators=(",", ":")),
            json.dumps(candidate_hashes, sort_keys=True, separators=(",", ":")),
            _comparison_classification(
                item.baseline_status,
                item.candidate_status,
                baseline_hashes,
                candidate_hashes,
            ),
            max(0.0, min(1.0, float(item.citation_coverage))),
            int(bool(item.unsafe_update)),
            int(bool(item.review_required)),
            max(0, int(item.review_reason_count)),
            max(0, int(item.accepted_citations)),
            max(0, int(item.total_citations)),
            max(0, int(item.rejected_citations)),
            item.model,
            max(0, int(item.latency_ms)),
            max(0.0, float(item.queue_lag_ms)),
            item.error_category,
        ),
    )
    return observation_id


def record_email_project_truth_shadow_fail_open(
    item: EmailProjectTruthShadowRecord,
) -> str | None:
    """Persist independently without delaying or failing inbound email work."""
    try:
        from src.db.connection import scoped_observability_write

        with scoped_observability_write() as conn:
            return record_email_project_truth_shadow(conn, item)
    except Exception:
        return None


def summarize_email_project_truth_shadow(
    conn: sqlite3.Connection,
) -> dict[str, object]:
    """Return aggregate-only rollout evidence with no email payload values."""
    empty: dict[str, object] = {
        "total": 0,
        "completed": 0,
        "failed": 0,
        "unsafe_updates": 0,
        "review_required": 0,
        "citation_coverage_below_100": 0,
        "accepted_citations": 0,
        "total_citations": 0,
        "rejected_citations": 0,
        "average_latency_ms": 0.0,
        "max_latency_ms": 0,
        "average_queue_lag_ms": 0.0,
        "max_queue_lag_ms": 0.0,
        "correlated_parent_runs": 0,
        "latest_created_at": None,
        "by_classification": {},
        "by_error_category": {},
        "by_model": {},
    }
    try:
        row = conn.execute(
            """SELECT COUNT(*) AS total,
                      SUM(CASE WHEN candidate_status='completed' THEN 1 ELSE 0 END) AS completed,
                      SUM(CASE WHEN candidate_status!='completed' THEN 1 ELSE 0 END) AS failed,
                      SUM(unsafe_update) AS unsafe_updates,
                      SUM(review_required) AS review_required,
                      SUM(CASE WHEN citation_coverage < 1.0 THEN 1 ELSE 0 END) AS citation_coverage_below_100,
                      SUM(accepted_citations) AS accepted_citations,
                      SUM(total_citations) AS total_citations,
                      SUM(rejected_citations) AS rejected_citations,
                      AVG(latency_ms) AS average_latency_ms,
                      MAX(latency_ms) AS max_latency_ms,
                      AVG(queue_lag_ms) AS average_queue_lag_ms,
                      MAX(queue_lag_ms) AS max_queue_lag_ms,
                      SUM(CASE WHEN parent_run_id IS NOT NULL AND shadow_run_id IS NOT NULL THEN 1 ELSE 0 END) AS correlated_parent_runs,
                      MAX(created_at) AS latest_created_at
                 FROM email_project_truth_shadow"""
        ).fetchone()
        if row is None:
            return empty
        values = dict(row) if hasattr(row, "keys") else dict(
            zip(
                (
                    "total", "completed", "failed", "unsafe_updates",
                    "review_required", "citation_coverage_below_100",
                    "accepted_citations", "total_citations",
                    "rejected_citations", "average_latency_ms",
                    "max_latency_ms", "average_queue_lag_ms",
                    "max_queue_lag_ms", "correlated_parent_runs",
                    "latest_created_at",
                ),
                row,
            )
        )
        summary = dict(empty)
        for name in (
            "total", "completed", "failed", "unsafe_updates",
            "review_required", "citation_coverage_below_100",
            "accepted_citations", "total_citations",
            "rejected_citations", "max_latency_ms",
            "correlated_parent_runs",
        ):
            summary[name] = int(values.get(name) or 0)
        for name in (
            "average_latency_ms", "average_queue_lag_ms",
            "max_queue_lag_ms",
        ):
            summary[name] = round(float(values.get(name) or 0.0), 3)
        summary["latest_created_at"] = values.get("latest_created_at")
        summary["by_classification"] = _aggregate_counts(
            conn, "comparison_classification"
        )
        summary["by_error_category"] = _aggregate_counts(
            conn, "error_category", exclude_null=True
        )
        summary["by_model"] = _aggregate_counts(
            conn, "model", exclude_null=True
        )
        return summary
    except (sqlite3.Error, AttributeError, TypeError):
        return empty


def _aggregate_counts(
    conn: sqlite3.Connection,
    column: str,
    *,
    exclude_null: bool = False,
) -> dict[str, int]:
    allowed = {"comparison_classification", "error_category", "model"}
    if column not in allowed:
        raise ValueError("unsupported aggregate column")
    where = f" WHERE {column} IS NOT NULL" if exclude_null else ""
    rows = conn.execute(
        f"SELECT {column} AS label, COUNT(*) AS count "
        f"FROM email_project_truth_shadow{where} GROUP BY {column}"
    ).fetchall()
    result: dict[str, int] = {}
    for row in rows:
        label = row["label"] if hasattr(row, "keys") else row[0]
        count = row["count"] if hasattr(row, "keys") else row[1]
        if label is not None:
            result[str(label)] = int(count)
    return result


def _field_hashes(fields: Mapping[str, str]) -> dict[str, str]:
    return {
        str(name): _hash_text(str(value))
        for name, value in sorted(fields.items(), key=lambda item: str(item[0]))
    }


def _comparison_classification(
    baseline_status: str,
    candidate_status: str,
    baseline_hashes: Mapping[str, str],
    candidate_hashes: Mapping[str, str],
) -> str:
    if candidate_status != "completed":
        return "candidate_error"
    if baseline_status != "completed":
        return "baseline_error"
    if baseline_hashes == candidate_hashes:
        return "exact"
    if set(baseline_hashes) == set(candidate_hashes):
        return "same_fields_different_values"
    if not baseline_hashes and candidate_hashes:
        return "candidate_only"
    if baseline_hashes and not candidate_hashes:
        return "baseline_only"
    return "different_fields"


def _hash_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


__all__ = [
    "EMAIL_PROJECT_TRUTH_SHADOW_POLICY_VERSION",
    "EmailProjectTruthShadowRecord",
    "record_email_project_truth_shadow",
    "record_email_project_truth_shadow_fail_open",
    "summarize_email_project_truth_shadow",
]
