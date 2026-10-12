"""Plaintext-free counterfactual replay for grounding-repair shadow policies."""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from src.assistant.evals.artifacts import stamp_content_hash, verify_content_hash
from src.assistant.evals.grounding_repair import (
    GroundingRepairReviewBatch,
    GroundingRepairShadowCohort,
    GroundingRepairShadowObservation,
    verify_grounding_repair_cohort_content_hash,
)
from src.assistant.grounding_repair_shadow import (
    GROUNDING_REPAIR_SHADOW_POLICY_VERSION,
    legacy_no_repair_decision,
)
from src.assistant.grounding_repair_shadow_fast import (
    GroundingRepairShadowProfile,
    evaluate_grounding_repair_shadow_fast,
)
from src.observability.payload_crypto import decrypt_payload

_CITATION_RE = re.compile(
    r"\[\[source:([A-Za-z0-9][A-Za-z0-9_.:-]*)\]\]"
)


def _answer_hash(value: str) -> str:
    encoded = json.dumps(
        value, ensure_ascii=False, sort_keys=True, default=str
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _database_hash(path: Path) -> str:
    hasher = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            hasher.update(chunk)
    return "sha256:" + hasher.hexdigest()


def _decrypt_payload(row: sqlite3.Row) -> dict[str, Any]:
    if not row["payload_ciphertext"] or not row["payload_nonce"]:
        raise ValueError(f"encrypted payload unavailable for {row['event_id']}")
    payload = decrypt_payload(
        str(row["payload_ciphertext"]),
        str(row["payload_nonce"]),
        str(row["payload_key_version"] or "v1"),
    )
    if not isinstance(payload, dict):
        raise ValueError(f"encrypted payload is not an object for {row['event_id']}")
    return payload


def _event_rows(
    database_path: Path,
    case_ids: list[str],
) -> dict[str, sqlite3.Row]:
    uri = f"{database_path.resolve().as_uri()}?mode=ro"
    conn = sqlite3.connect(uri, uri=True, timeout=30)
    conn.row_factory = sqlite3.Row
    try:
        placeholders = ",".join("?" for _ in case_ids)
        rows = conn.execute(
            f"SELECT * FROM trace_events WHERE event_type='grounding.repair.shadow' "
            f"AND event_id IN ({placeholders})",
            case_ids,
        ).fetchall()
    finally:
        conn.close()
    by_id = {str(row["event_id"]): row for row in rows}
    if set(by_id) != set(case_ids):
        missing = sorted(set(case_ids) - set(by_id))
        raise ValueError(f"grounding replay event lineage missing: {missing}")
    return by_id


def replay_grounding_repair_policy_v2(
    database_path: Path,
    source_cohort: GroundingRepairShadowCohort,
    *,
    cohort_id: str,
) -> GroundingRepairShadowCohort:
    """Reclassify an existing cohort with v2 while retaining no plaintext."""
    if not verify_grounding_repair_cohort_content_hash(source_cohort):
        raise ValueError("source cohort content hash is invalid")
    database = Path(database_path).resolve()
    if not database.is_file():
        raise FileNotFoundError(database)

    case_ids = [item.case_id for item in source_cohort.observations]
    rows = _event_rows(database, case_ids)
    replayed: list[GroundingRepairShadowObservation] = []
    for source in source_cohort.observations:
        row = rows[source.case_id]
        if (
            source.trace_payload_hash
            and str(row["payload_hash"] or "") != source.trace_payload_hash
        ):
            raise ValueError(f"trace payload lineage mismatch for {source.case_id}")
        payload = _decrypt_payload(row)
        prepared = str(payload.get("prepared_answer") or "")
        candidate = str(payload.get("candidate_answer") or "")
        baseline = str(payload.get("baseline_answer") or "")
        for label, value, expected in (
            ("prepared", prepared, source.prepared_answer_hash),
            ("candidate", candidate, source.candidate_answer_hash),
            ("baseline", baseline, source.baseline_answer_hash),
        ):
            if not expected or _answer_hash(value) != expected:
                raise ValueError(f"{label} answer lineage mismatch for {source.case_id}")

        sources = [
            item for item in payload.get("sources") or [] if isinstance(item, dict)
        ]
        tool_results = [
            item for item in payload.get("tool_results") or [] if isinstance(item, dict)
        ]
        source_ids = {
            str(item.get("source_id") or "").strip()
            for item in sources
            if str(item.get("source_id") or "").strip()
        }
        factual_tools_used = any(item.get("tool") != "think" for item in tool_results)

        profile = GroundingRepairShadowProfile(
            semantic_support_strategy=(
                "lazy_first_risk"
                if source.baseline_repair_attempted
                else "not_evaluated"
            )
        )
        if source.baseline_repair_attempted:
            total_started_ns = time.perf_counter_ns()
            decision = evaluate_grounding_repair_shadow_fast(
                prepared_answer=prepared,
                initial_error_codes=source.initial_error_codes,
                candidate_answer=candidate,
                candidate_error_codes=source.candidate_error_codes,
                source_ids=source_ids,
                sources=sources,
                factual_tools_used=factual_tools_used,
                profile=profile,
            )
            compute_ms = (time.perf_counter_ns() - total_started_ns) / 1_000_000
            support_compute_ms = profile.source_support_compute_ms
            decision_compute_ms = max(0.0, compute_ms - support_compute_ms)
            semantic_support_precomputed = False
        else:
            decision = legacy_no_repair_decision()
            compute_ms = 0.0
            support_compute_ms = 0.0
            decision_compute_ms = 0.0
            semantic_support_precomputed = False

        cited = set(_CITATION_RE.findall(candidate))
        citation_ids_valid = not (cited - source_ids)
        required_citation = (
            not factual_tools_used or not source_ids or bool(cited & source_ids)
        )
        projected_savings = (
            max(0.0, source.baseline_ready_ms - source.candidate_ready_ms)
            if decision.would_skip_repair else 0.0
        )
        replayed.append(source.model_copy(update={
            "policy_version": GROUNDING_REPAIR_SHADOW_POLICY_VERSION,
            "decision": decision.decision,
            "reason": decision.reason,
            "would_skip_repair": decision.would_skip_repair,
            "candidate_citation_ids_valid": citation_ids_valid,
            "candidate_has_required_citation": required_citation,
            "all_valid_cited_lines_preserved": decision.all_valid_cited_lines_preserved,
            "substantive_line_retention": decision.substantive_line_retention,
            "shadow_compute_ms": compute_ms,
            "shadow_source_support_compute_ms": support_compute_ms,
            "shadow_decision_compute_ms": decision_compute_ms,
            "semantic_support_precomputed": semantic_support_precomputed,
            **profile.as_metadata(),
            "projected_completion_savings_ms": projected_savings,
            "policy_replay_match": True,
        }))

    cohort = GroundingRepairShadowCohort(
        cohort_id=cohort_id,
        created_at=datetime.now(timezone.utc),
        source_database_hash=_database_hash(database),
        collection_class="plaintext_free_counterfactual_policy_v2_replay",
        source_cohort_hashes=[source_cohort.content_hash],
        observations=replayed,
    )
    return stamp_content_hash(cohort)


def relineage_grounding_repair_reviews(
    source_reviews: GroundingRepairReviewBatch,
    source_cohort: GroundingRepairShadowCohort,
    target_cohort: GroundingRepairShadowCohort,
) -> GroundingRepairReviewBatch:
    """Reuse scores and issue taxonomies when answer hashes are unchanged."""
    if not verify_grounding_repair_cohort_content_hash(source_cohort):
        raise ValueError("source cohort content hash is invalid")
    if not verify_grounding_repair_cohort_content_hash(target_cohort):
        raise ValueError("target cohort content hash is invalid")
    if not verify_content_hash(source_reviews):
        raise ValueError("source review content hash is invalid")
    if (
        source_reviews.cohort_id != source_cohort.cohort_id
        or source_reviews.cohort_hash != source_cohort.content_hash
    ):
        raise ValueError("source reviews do not reference the source cohort")
    target_by_id = {item.case_id: item for item in target_cohort.observations}
    reviews = []
    for review in source_reviews.reviews:
        observation = target_by_id.get(review.case_id)
        if observation is None:
            raise ValueError(f"target cohort is missing review case {review.case_id}")
        if (
            review.candidate_answer_hash != observation.candidate_answer_hash
            or review.baseline_answer_hash != observation.baseline_answer_hash
        ):
            raise ValueError(f"answer hash changed for review case {review.case_id}")
        reviews.append(review.model_copy(update={"notes": ""}))
    return stamp_content_hash(
        GroundingRepairReviewBatch(
            cohort_id=target_cohort.cohort_id,
            cohort_hash=target_cohort.content_hash,
            created_at=datetime.now(timezone.utc),
            reviews=reviews,
        )
    )


__all__ = [
    "relineage_grounding_repair_reviews",
    "replay_grounding_repair_policy_v2",
]
