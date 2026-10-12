"""Replay artifacts and evidence gates for grounding repair shadow observations."""

from __future__ import annotations

import hashlib
import json
import random
import re
import sqlite3
import statistics
from collections import Counter
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from src.assistant.evals.artifacts import content_hash, stamp_content_hash, verify_content_hash, write_model
from src.assistant.grounding_repair_shadow import (
    GROUNDING_REPAIR_SHADOW_POLICY_V1,
    GROUNDING_REPAIR_SHADOW_POLICY_VERSION,
    build_grounding_semantic_source_support,
    evaluate_grounding_repair_shadow,
    evaluate_grounding_repair_shadow_v1,
    legacy_no_repair_decision,
)
from src.observability.payload_crypto import decrypt_payload

_CITATION_RE = re.compile(r"\[\[source:([A-Za-z0-9][A-Za-z0-9_.:-]*)\]\]")
_ISSUE_CODES = frozenset({
    "citation_id_error",
    "unsupported_claim",
    "scope_error",
    "as_of_error",
    "critical_claim_error",
})


class GroundingReplayModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class EvidenceOrigin(str, Enum):
    REAL = "real"
    PRODUCTION_SHAPED = "production_shaped"
    CONTROLLED = "controlled"
    SYNTHETIC = "synthetic"


class GroundingRepairSelectionItem(GroundingReplayModel):
    case_id: str = Field(min_length=1)
    conversation_id: str | None = None
    question_turn_id: str | None = None
    question_turn_index: int | None = Field(default=None, ge=1)
    question_hash: str | None = None
    request_hash: str
    attempt_id: str | None = None
    thread_id: str | None = None
    turn_id: str = Field(min_length=1)
    run_id: str = Field(min_length=1)
    trace_id: str | None = None
    event_id: str = Field(min_length=1)


class GroundingRepairSelectionManifest(GroundingReplayModel):
    schema_version: Literal[1] = 1
    generated_at: datetime
    collection_class: str = Field(min_length=3)
    evidence_origin: Literal["controlled"] = "controlled"
    batch_id: str = Field(min_length=1)
    phase: str | None = None
    question_set_id: str | None = None
    question_set_content_hash: str | None = None
    expected_turn_count: int = Field(ge=1)
    selected_turn_count: int = Field(ge=0)
    complete: bool
    selected: list[GroundingRepairSelectionItem]
    content_hash: str = ""

    @field_validator("generated_at")
    @classmethod
    def generated_at_is_aware(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("selection generated_at must be timezone-aware")
        return value

    @model_validator(mode="after")
    def selection_is_exact(self) -> "GroundingRepairSelectionManifest":
        if self.selected_turn_count != len(self.selected):
            raise ValueError("selected_turn_count does not match selected entries")
        if self.selected_turn_count > self.expected_turn_count:
            raise ValueError("selected turns exceed expected turns")
        if self.complete != (self.selected_turn_count == self.expected_turn_count):
            raise ValueError("selection complete flag does not match selected count")
        for label, values in {
            "turn_id": [item.turn_id for item in self.selected],
            "run_id": [item.run_id for item in self.selected],
            "event_id": [item.event_id for item in self.selected],
            "case_id": [item.case_id for item in self.selected],
        }.items():
            if len(values) != len(set(values)):
                raise ValueError(f"selection contains duplicate {label} values")
        return self


class GroundingRepairShadowObservation(GroundingReplayModel):
    schema_version: Literal[1] = 1
    case_id: str = Field(min_length=1)
    run_id: str = Field(min_length=1)
    trace_id: str | None = None
    turn_id: str | None = None
    surface: str | None = None
    observed_at: datetime
    origin: EvidenceOrigin
    policy_version: str
    decision: str
    reason: str
    initial_error_codes: list[str] = Field(default_factory=list)
    candidate_error_codes: list[str] = Field(default_factory=list)
    baseline_error_codes: list[str] = Field(default_factory=list)
    error_signature: str
    request_hash: str | None = None
    prepared_answer_hash: str | None = None
    candidate_answer_hash: str | None = None
    baseline_answer_hash: str | None = None
    trace_payload_hash: str | None = None
    baseline_repair_attempted: bool
    would_skip_repair: bool
    candidate_citation_ids_valid: bool
    candidate_has_required_citation: bool
    all_valid_cited_lines_preserved: bool
    substantive_line_retention: float = Field(ge=0.0)
    shadow_compute_ms: float = Field(ge=0.0)
    shadow_source_support_compute_ms: float | None = Field(default=None, ge=0.0)
    shadow_decision_compute_ms: float | None = Field(default=None, ge=0.0)
    semantic_support_precomputed: bool | None = None
    semantic_support_strategy: str | None = None
    semantic_support_materialized_count: int | None = Field(default=None, ge=0)
    semantic_line_count: int | None = Field(default=None, ge=0)
    semantic_lines_evaluated: int | None = Field(default=None, ge=0)
    semantic_analysis_complete: bool | None = None
    semantic_short_circuit: bool | None = None
    candidate_ready_ms: float = Field(ge=0.0)
    baseline_ready_ms: float = Field(ge=0.0)
    projected_completion_savings_ms: float = Field(ge=0.0)
    served_answer_source: str | None = None
    shadow_additional_llm_calls: int | None = None
    shadow_synchronous_db_writes: int | None = None
    user_visible_behavior_changed: bool | None = None
    business_persistence_changed: bool | None = None
    policy_replay_match: bool
    data_complete: bool
    incomplete_reasons: list[str] = Field(default_factory=list)

    @field_validator("observed_at")
    @classmethod
    def observed_at_is_aware(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("observed_at must be timezone-aware")
        return value


class GroundingRepairShadowCohort(GroundingReplayModel):
    schema_version: Literal[1] = 1
    cohort_id: str = Field(min_length=3)
    created_at: datetime
    policy_version: str = GROUNDING_REPAIR_SHADOW_POLICY_VERSION
    source_database_hash: str
    observed_after: datetime | None = None
    observed_before: datetime | None = None
    collection_class: str | None = None
    selection_manifest_hash: str | None = None
    source_cohort_hashes: list[str] = Field(default_factory=list)
    observations: list[GroundingRepairShadowObservation] = Field(default_factory=list)
    content_hash: str = ""

    @field_validator("created_at", "observed_after", "observed_before")
    @classmethod
    def cohort_timestamps_are_aware(cls, value: datetime | None) -> datetime | None:
        if value is not None and (value.tzinfo is None or value.utcoffset() is None):
            raise ValueError("cohort timestamps must be timezone-aware")
        return value

    @model_validator(mode="after")
    def cohort_window_is_ordered(self) -> "GroundingRepairShadowCohort":
        if (
            self.observed_after is not None
            and self.observed_before is not None
            and self.observed_after >= self.observed_before
        ):
            raise ValueError("observed_after must be earlier than observed_before")
        return self


_LEGACY_COHORT_HASH_EXCLUDED_FIELDS = frozenset({
    "collection_class",
    "selection_manifest_hash",
    "source_cohort_hashes",
})
_LEGACY_OBSERVATION_HASH_EXCLUDED_FIELDS = frozenset({
    "shadow_source_support_compute_ms",
    "shadow_decision_compute_ms",
    "semantic_support_precomputed",
    "semantic_support_strategy",
    "semantic_support_materialized_count",
    "semantic_line_count",
    "semantic_lines_evaluated",
    "semantic_analysis_complete",
    "semantic_short_circuit",
})


def verify_grounding_repair_cohort_content_hash(
    cohort: GroundingRepairShadowCohort,
) -> bool:
    """Verify current cohorts and backward-compatible plaintext-free artifacts."""
    if verify_content_hash(cohort):
        return True

    legacy_projection = cohort.model_dump(mode="json")
    changed = False
    for field in _LEGACY_COHORT_HASH_EXCLUDED_FIELDS:
        if field in cohort.model_fields_set:
            continue
        value = getattr(cohort, field)
        if value not in (None, []):
            return False
        legacy_projection.pop(field, None)
        changed = True

    serialized_observations = legacy_projection.get("observations") or []
    for observation, serialized in zip(cohort.observations, serialized_observations):
        for field in _LEGACY_OBSERVATION_HASH_EXCLUDED_FIELDS:
            if field in observation.model_fields_set:
                continue
            if getattr(observation, field) is not None:
                return False
            serialized.pop(field, None)
            changed = True

    stored = str(cohort.content_hash or "")
    return changed and bool(stored) and stored == content_hash(legacy_projection)


class ReviewStatus(str, Enum):
    PENDING = "pending"
    REVIEWED = "reviewed"


class GroundingRepairCaseReview(GroundingReplayModel):
    case_id: str = Field(min_length=1)
    candidate_answer_hash: str
    baseline_answer_hash: str
    status: ReviewStatus = ReviewStatus.PENDING
    reviewer: str | None = None
    reviewed_at: datetime | None = None
    candidate_score: float | None = Field(default=None, ge=0.0, le=100.0)
    baseline_score: float | None = Field(default=None, ge=0.0, le=100.0)
    candidate_pass: bool | None = None
    baseline_pass: bool | None = None
    candidate_issue_codes: list[str] = Field(default_factory=list)
    baseline_issue_codes: list[str] = Field(default_factory=list)
    notes: str = ""

    @field_validator("candidate_issue_codes", "baseline_issue_codes")
    @classmethod
    def validate_issue_codes(cls, values: list[str]) -> list[str]:
        normalized = list(dict.fromkeys(str(item).strip() for item in values if str(item).strip()))
        unknown = sorted(set(normalized) - _ISSUE_CODES)
        if unknown:
            raise ValueError(f"unknown review issue codes: {', '.join(unknown)}")
        return normalized

    @model_validator(mode="after")
    def reviewed_entry_is_complete(self) -> "GroundingRepairCaseReview":
        if self.status == ReviewStatus.PENDING:
            return self
        required = (
            self.reviewer,
            self.reviewed_at,
            self.candidate_score,
            self.baseline_score,
            self.candidate_pass,
            self.baseline_pass,
        )
        if any(value is None or value == "" for value in required):
            raise ValueError("reviewed entries require reviewer, timestamp, scores, and pass decisions")
        if self.reviewed_at is not None and (
            self.reviewed_at.tzinfo is None or self.reviewed_at.utcoffset() is None
        ):
            raise ValueError("reviewed_at must be timezone-aware")
        return self


class GroundingRepairReviewBatch(GroundingReplayModel):
    schema_version: Literal[1] = 1
    cohort_id: str
    cohort_hash: str
    created_at: datetime
    reviews: list[GroundingRepairCaseReview] = Field(default_factory=list)
    content_hash: str = ""

    @field_validator("created_at")
    @classmethod
    def review_created_at_is_aware(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("created_at must be timezone-aware")
        return value


class GroundingRepairGateReport(GroundingReplayModel):
    schema_version: Literal[1] = 1
    cohort_id: str
    cohort_hash: str
    review_hash: str | None = None
    generated_at: datetime
    metrics: dict[str, Any]
    gates: dict[str, bool]
    all_passed: bool
    content_hash: str = ""



def _json_object(value: Any) -> dict[str, Any]:
    if not isinstance(value, str) or not value:
        return {}
    try:
        parsed = json.loads(value)
    except (TypeError, ValueError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _hash_text(value: str) -> str:
    return "sha256:" + hashlib.sha256(value.encode("utf-8")).hexdigest()


def _answer_hash(value: str) -> str:
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, default=str).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _parse_timestamp(value: Any) -> datetime:
    parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def _decrypt_event_row(row: sqlite3.Row | dict[str, Any]) -> tuple[dict[str, Any] | None, str | None]:
    item = dict(row)
    if not item.get("payload_ciphertext") or not item.get("payload_nonce"):
        return None, "payload_unavailable"
    try:
        payload = decrypt_payload(
            str(item["payload_ciphertext"]),
            str(item["payload_nonce"]),
            str(item.get("payload_key_version") or "v1"),
        )
    except Exception as exc:
        return None, f"payload_decrypt_failed:{type(exc).__name__}"
    if not isinstance(payload, dict):
        return None, "payload_not_object"
    return payload, None


def _request_hashes(
    conn: sqlite3.Connection,
    run_ids: list[str],
) -> dict[str, str]:
    if not run_ids:
        return {}
    placeholders = ",".join("?" for _ in run_ids)
    rows = conn.execute(
        f"SELECT * FROM trace_events WHERE event_type='run.start' "
        f"AND run_id IN ({placeholders}) ORDER BY run_id, sequence",
        run_ids,
    ).fetchall()
    values: dict[str, str] = {}
    for row in rows:
        run_id = str(row["run_id"])
        if run_id in values:
            continue
        payload, _ = _decrypt_event_row(row)
        user_input = payload.get("user_input") if isinstance(payload, dict) else None
        if isinstance(user_input, str) and user_input.strip():
            values[run_id] = _hash_text(user_input)
    return values


def _database_hash(path: Path) -> str:
    hasher = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            hasher.update(chunk)
    return "sha256:" + hasher.hexdigest()


def collect_grounding_repair_shadow_cohort(
    database_path: Path,
    *,
    cohort_id: str,
    origin: EvidenceOrigin,
    include_incomplete: bool = True,
    observed_after: datetime | None = None,
    observed_before: datetime | None = None,
    selection: GroundingRepairSelectionManifest | None = None,
) -> GroundingRepairShadowCohort:
    """Decrypt shadow events locally, replay policy, and retain only hashed artifacts."""
    db_path = Path(database_path).resolve()
    if not db_path.is_file():
        raise FileNotFoundError(db_path)
    for label, value in (("observed_after", observed_after), ("observed_before", observed_before)):
        if value is not None and (value.tzinfo is None or value.utcoffset() is None):
            raise ValueError(f"{label} must be timezone-aware")
    if observed_after is not None and observed_before is not None and observed_after >= observed_before:
        raise ValueError("observed_after must be earlier than observed_before")
    if selection is not None:
        if observed_after is not None or observed_before is not None:
            raise ValueError("exact selection cannot be combined with an observed time window")
        if not verify_content_hash(selection):
            raise ValueError("selection manifest content_hash is missing or invalid")
        if not selection.complete:
            raise ValueError("selection manifest is incomplete")

    uri = f"{db_path.as_uri()}?mode=ro"
    conn = sqlite3.connect(uri, uri=True, timeout=30)
    conn.row_factory = sqlite3.Row
    try:
        where = ["e.event_type='grounding.repair.shadow'"]
        parameters: list[str] = []
        if observed_after is not None:
            where.append("julianday(e.created_at) >= julianday(?)")
            parameters.append(observed_after.astimezone(timezone.utc).isoformat())
        if observed_before is not None:
            where.append("julianday(e.created_at) < julianday(?)")
            parameters.append(observed_before.astimezone(timezone.utc).isoformat())
        if selection is not None:
            event_ids = [item.event_id for item in selection.selected]
            placeholders = ",".join("?" for _ in event_ids)
            where.append(f"e.event_id IN ({placeholders})")
            parameters.extend(event_ids)
        rows = conn.execute(
            f"""SELECT e.*, r.surface
                FROM trace_events e
                JOIN trace_runs r ON r.run_id=e.run_id
                WHERE {' AND '.join(where)}
                ORDER BY e.created_at, e.run_id, e.sequence""",
            parameters,
        ).fetchall()
        selected_by_event = (
            {item.event_id: item for item in selection.selected}
            if selection is not None else {}
        )
        if selection is not None:
            actual_event_ids = {str(row["event_id"]) for row in rows}
            expected_event_ids = set(selected_by_event)
            if actual_event_ids != expected_event_ids:
                missing = sorted(expected_event_ids - actual_event_ids)
                extra = sorted(actual_event_ids - expected_event_ids)
                raise ValueError(f"selection event lineage mismatch: missing={missing}, extra={extra}")
            for row in rows:
                selected_item = selected_by_event[str(row["event_id"])]
                if (
                    str(row["run_id"]) != selected_item.run_id
                    or str(row["turn_id"] or "") != selected_item.turn_id
                    or str(row["trace_id"] or "") != str(selected_item.trace_id or "")
                ):
                    raise ValueError(f"selection run/turn lineage mismatch for {selected_item.event_id}")
            selection_order = {item.event_id: index for index, item in enumerate(selection.selected)}
            rows = sorted(rows, key=lambda row: selection_order[str(row["event_id"])])
        request_hashes = _request_hashes(conn, sorted({str(row["run_id"]) for row in rows}))
        observations: list[GroundingRepairShadowObservation] = []
        for row in rows:
            metadata = _json_object(row["metadata_json"])
            payload, payload_error = _decrypt_event_row(row)
            payload = payload or {}
            prepared = str(payload.get("prepared_answer") or "")
            candidate = str(payload.get("candidate_answer") or "")
            baseline = str(payload.get("baseline_answer") or "")
            sources = payload.get("sources") if isinstance(payload.get("sources"), list) else []
            tool_results = (
                payload.get("tool_results") if isinstance(payload.get("tool_results"), list) else []
            )
            source_ids = {
                str(source.get("source_id") or "").strip()
                for source in sources
                if isinstance(source, dict) and str(source.get("source_id") or "").strip()
            }
            cited = set(_CITATION_RE.findall(candidate))
            factual_tools_used = any(
                isinstance(item, dict) and item.get("tool") != "think" for item in tool_results
            )
            citation_ids_valid = not (cited - source_ids)
            required_citation = not factual_tools_used or not source_ids or bool(cited & source_ids)
            initial_codes = [str(item) for item in metadata.get("initial_error_codes") or []]
            candidate_codes = [str(item) for item in metadata.get("candidate_error_codes") or []]
            baseline_counts = metadata.get("baseline_error_counts")
            baseline_codes = []
            if isinstance(baseline_counts, dict):
                baseline_codes = [
                    str(code)
                    for code, count in baseline_counts.items()
                    for _ in range(max(0, int(count or 0)))
                ]
            if metadata.get("decision") == "legacy_no_repair":
                replay = legacy_no_repair_decision()
            elif metadata.get("policy_version") == GROUNDING_REPAIR_SHADOW_POLICY_V1:
                replay = evaluate_grounding_repair_shadow_v1(
                    prepared_answer=prepared,
                    initial_error_codes=initial_codes,
                    candidate_answer=candidate,
                    candidate_error_codes=candidate_codes,
                    source_ids=source_ids,
                    factual_tools_used=factual_tools_used,
                )
            else:
                semantic_source_support = build_grounding_semantic_source_support(sources)
                replay = evaluate_grounding_repair_shadow(
                    prepared_answer=prepared,
                    initial_error_codes=initial_codes,
                    candidate_answer=candidate,
                    candidate_error_codes=candidate_codes,
                    source_ids=source_ids,
                    sources=sources,
                    semantic_source_support=semantic_source_support,
                    factual_tools_used=factual_tools_used,
                )
            replay_match = (
                replay.decision == metadata.get("decision")
                and replay.reason == metadata.get("reason")
                and bool(replay.would_skip_repair) == bool(metadata.get("would_skip_repair"))
            )

            incomplete: list[str] = []
            if payload_error:
                incomplete.append(payload_error)
            request_hash = request_hashes.get(str(row["run_id"]))
            if request_hash is None:
                incomplete.append("request_hash_unavailable")
            selected_item = selected_by_event.get(str(row["event_id"]))
            if selected_item is not None and request_hash != selected_item.request_hash:
                incomplete.append("selection_request_hash_mismatch")
            expected_hashes = {
                "prepared_answer_hash": (prepared, metadata.get("prepared_answer_hash")),
                "candidate_answer_hash": (candidate, metadata.get("candidate_answer_hash")),
                "baseline_answer_hash": (baseline, metadata.get("baseline_answer_hash")),
            }
            for label, (answer, expected) in expected_hashes.items():
                if not expected:
                    incomplete.append(f"{label}_missing")
                elif _answer_hash(answer) != str(expected):
                    incomplete.append(f"{label}_mismatch")
            if not replay_match:
                incomplete.append("policy_replay_mismatch")
            if metadata.get("decision") == "shadow_error":
                incomplete.append("shadow_evaluation_error")
            for field in ("shadow_compute_ms", "candidate_ready_ms", "baseline_ready_ms"):
                if metadata.get(field) is None:
                    incomplete.append(f"{field}_missing")

            signature = "+".join(sorted(set(initial_codes))) or "control"
            observation = GroundingRepairShadowObservation(
                case_id=str(row["event_id"]),
                run_id=str(row["run_id"]),
                trace_id=str(row["trace_id"] or "") or None,
                turn_id=str(row["turn_id"] or "") or None,
                surface=str(row["surface"] or "") or None,
                observed_at=_parse_timestamp(row["created_at"]),
                origin=origin,
                policy_version=str(metadata.get("policy_version") or ""),
                decision=str(metadata.get("decision") or "unknown"),
                reason=str(metadata.get("reason") or "unknown"),
                initial_error_codes=initial_codes,
                candidate_error_codes=candidate_codes,
                baseline_error_codes=baseline_codes,
                error_signature=signature,
                request_hash=request_hash,
                prepared_answer_hash=str(metadata.get("prepared_answer_hash") or "") or None,
                candidate_answer_hash=str(metadata.get("candidate_answer_hash") or "") or None,
                baseline_answer_hash=str(metadata.get("baseline_answer_hash") or "") or None,
                trace_payload_hash=str(row["payload_hash"] or "") or None,
                baseline_repair_attempted=bool(metadata.get("baseline_repair_attempted")),
                would_skip_repair=bool(metadata.get("would_skip_repair")),
                candidate_citation_ids_valid=citation_ids_valid,
                candidate_has_required_citation=required_citation,
                all_valid_cited_lines_preserved=bool(
                    metadata.get("all_valid_cited_lines_preserved", False)
                ),
                substantive_line_retention=float(
                    metadata.get("substantive_line_retention") or 0.0
                ),
                shadow_compute_ms=float(metadata.get("shadow_compute_ms") or 0.0),
                shadow_source_support_compute_ms=(
                    float(metadata["shadow_source_support_compute_ms"])
                    if metadata.get("shadow_source_support_compute_ms") is not None
                    else None
                ),
                shadow_decision_compute_ms=(
                    float(metadata["shadow_decision_compute_ms"])
                    if metadata.get("shadow_decision_compute_ms") is not None
                    else None
                ),
                semantic_support_precomputed=(
                    bool(metadata["semantic_support_precomputed"])
                    if metadata.get("semantic_support_precomputed") is not None
                    else None
                ),
                semantic_support_strategy=(
                    str(metadata["semantic_support_strategy"])
                    if metadata.get("semantic_support_strategy") is not None
                    else None
                ),
                semantic_support_materialized_count=(
                    int(metadata["semantic_support_materialized_count"])
                    if metadata.get("semantic_support_materialized_count") is not None
                    else None
                ),
                semantic_line_count=(
                    int(metadata["semantic_line_count"])
                    if metadata.get("semantic_line_count") is not None
                    else None
                ),
                semantic_lines_evaluated=(
                    int(metadata["semantic_lines_evaluated"])
                    if metadata.get("semantic_lines_evaluated") is not None
                    else None
                ),
                semantic_analysis_complete=(
                    bool(metadata["semantic_analysis_complete"])
                    if metadata.get("semantic_analysis_complete") is not None
                    else None
                ),
                semantic_short_circuit=(
                    bool(metadata["semantic_short_circuit"])
                    if metadata.get("semantic_short_circuit") is not None
                    else None
                ),
                candidate_ready_ms=float(metadata.get("candidate_ready_ms") or 0.0),
                baseline_ready_ms=float(metadata.get("baseline_ready_ms") or 0.0),
                projected_completion_savings_ms=float(
                    metadata.get("projected_completion_savings_ms") or 0.0
                ),
                served_answer_source=metadata.get("served_answer_source"),
                shadow_additional_llm_calls=metadata.get("shadow_additional_llm_calls"),
                shadow_synchronous_db_writes=metadata.get("shadow_synchronous_db_writes"),
                user_visible_behavior_changed=metadata.get("user_visible_behavior_changed"),
                business_persistence_changed=metadata.get("business_persistence_changed"),
                policy_replay_match=replay_match,
                data_complete=not incomplete,
                incomplete_reasons=sorted(set(incomplete)),
            )
            if include_incomplete or observation.data_complete:
                observations.append(observation)
    finally:
        conn.close()

    return stamp_content_hash(GroundingRepairShadowCohort(
        cohort_id=cohort_id,
        created_at=datetime.now(timezone.utc),
        source_database_hash=_database_hash(db_path),
        observed_after=observed_after,
        observed_before=observed_before,
        collection_class=selection.collection_class if selection is not None else None,
        selection_manifest_hash=selection.content_hash if selection is not None else None,
        observations=observations,
    ))


def combine_grounding_repair_cohorts(
    cohorts: list[GroundingRepairShadowCohort],
    *,
    cohort_id: str,
    expected_observation_count: int | None = None,
) -> GroundingRepairShadowCohort:
    """Combine exact controlled cohorts without changing observation provenance."""
    if not cohorts:
        raise ValueError("at least one grounding repair cohort is required")
    if any(not verify_grounding_repair_cohort_content_hash(cohort) for cohort in cohorts):
        raise ValueError("all source cohorts must have valid content hashes")
    policy_versions = {cohort.policy_version for cohort in cohorts}
    if len(policy_versions) != 1:
        raise ValueError("source cohorts use different grounding repair policy versions")
    observations = [item for cohort in cohorts for item in cohort.observations]
    if expected_observation_count is not None and len(observations) != expected_observation_count:
        raise ValueError(
            f"combined cohort contains {len(observations)} observations; "
            f"expected {expected_observation_count}"
        )
    for label, values in {
        "case_id": [item.case_id for item in observations],
        "run_id": [item.run_id for item in observations],
        "turn_id": [str(item.turn_id or "") for item in observations],
    }.items():
        if len(values) != len(set(values)):
            raise ValueError(f"combined cohort contains duplicate {label} values")
    source_hashes = [cohort.content_hash for cohort in cohorts]
    classes = sorted({str(cohort.collection_class or "unclassified") for cohort in cohorts})
    return stamp_content_hash(GroundingRepairShadowCohort(
        cohort_id=cohort_id,
        created_at=datetime.now(timezone.utc),
        policy_version=next(iter(policy_versions)),
        source_database_hash=content_hash({
            "source_database_hashes": [cohort.source_database_hash for cohort in cohorts]
        }),
        collection_class=(classes[0] if len(classes) == 1 else "mixed_controlled_replay_and_simulation"),
        source_cohort_hashes=source_hashes,
        observations=observations,
    ))


def build_grounding_repair_review_template(
    cohort: GroundingRepairShadowCohort,
) -> GroundingRepairReviewBatch:
    """Build a plaintext-free review ledger keyed only by trace and answer hashes."""
    reviews = [
        GroundingRepairCaseReview(
            case_id=item.case_id,
            candidate_answer_hash=str(item.candidate_answer_hash or "missing"),
            baseline_answer_hash=str(item.baseline_answer_hash or "missing"),
        )
        for item in cohort.observations
        if item.data_complete and item.baseline_repair_attempted
    ]
    return stamp_content_hash(GroundingRepairReviewBatch(
        cohort_id=cohort.cohort_id,
        cohort_hash=cohort.content_hash,
        created_at=datetime.now(timezone.utc),
        reviews=reviews,
    ))


def seal_grounding_repair_reviews(
    batch: GroundingRepairReviewBatch,
) -> GroundingRepairReviewBatch:
    pending = [item.case_id for item in batch.reviews if item.status != ReviewStatus.REVIEWED]
    if pending:
        raise ValueError(f"review batch still has {len(pending)} pending entries")
    return stamp_content_hash(batch.model_copy(update={"content_hash": ""}))


def _percentile(values: list[float], percentile: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    index = max(0, min(len(ordered) - 1, int(round((len(ordered) - 1) * percentile))))
    return float(ordered[index])


def _bootstrap_lower_bound(differences: list[float], *, samples: int = 10_000) -> float | None:
    if not differences:
        return None
    rng = random.Random(20260809)
    size = len(differences)
    means = [
        statistics.fmean(differences[rng.randrange(size)] for _ in range(size))
        for _ in range(samples)
    ]
    return _percentile(means, 0.025)


def _new_candidate_issues(review: GroundingRepairCaseReview) -> set[str]:
    return set(review.candidate_issue_codes) - set(review.baseline_issue_codes)


def evaluate_grounding_repair_gates(
    cohort: GroundingRepairShadowCohort,
    reviews: GroundingRepairReviewBatch | None = None,
) -> GroundingRepairGateReport:
    """Evaluate the pre-declared Phase 2A non-inferiority and safety gates."""
    if not verify_grounding_repair_cohort_content_hash(cohort):
        raise ValueError("cohort content hash is invalid")
    if reviews is not None:
        if reviews.cohort_id != cohort.cohort_id or reviews.cohort_hash != cohort.content_hash:
            raise ValueError("review batch does not reference this cohort")
        if not verify_content_hash(reviews):
            raise ValueError("review batch content hash is invalid")

    qualifying = [
        item for item in cohort.observations
        if item.data_complete and item.origin != EvidenceOrigin.SYNTHETIC
    ]
    repair_cases = [item for item in qualifying if item.baseline_repair_attempted]
    controls = [item for item in qualifying if not item.baseline_repair_attempted]
    eligible = [item for item in repair_cases if item.would_skip_repair]
    signatures = sorted({item.error_signature for item in eligible})
    real_signature_counts = Counter(
        item.error_signature
        for item in eligible
        if item.origin == EvidenceOrigin.REAL
    )
    request_hashes = {item.request_hash for item in qualifying if item.request_hash}

    repair_reduction = len(eligible) / len(repair_cases) if repair_cases else 0.0
    eligible_savings = [item.projected_completion_savings_ms for item in eligible]
    median_savings = statistics.median(eligible_savings) if eligible_savings else 0.0
    shadow_p95 = _percentile([item.shadow_compute_ms for item in repair_cases], 0.95)
    source_support_p95 = _percentile(
        [
            item.shadow_source_support_compute_ms
            for item in repair_cases
            if item.shadow_source_support_compute_ms is not None
        ],
        0.95,
    )
    decision_p95 = _percentile(
        [
            item.shadow_decision_compute_ms
            for item in repair_cases
            if item.shadow_decision_compute_ms is not None
        ],
        0.95,
    )
    semantic_profile_cases = [
        item
        for item in repair_cases
        if item.semantic_support_strategy is not None
    ]
    materialized_source_p95 = _percentile(
        [
            float(item.semantic_support_materialized_count)
            for item in semantic_profile_cases
            if item.semantic_support_materialized_count is not None
        ],
        0.95,
    )
    semantic_analysis_cases = [
        item
        for item in semantic_profile_cases
        if (item.semantic_lines_evaluated or 0) > 0
        or item.semantic_analysis_complete is True
    ]
    semantic_analysis_rate = (
        len(semantic_analysis_cases) / len(semantic_profile_cases)
        if semantic_profile_cases else None
    )
    semantic_short_circuit_rate = (
        sum(item.semantic_short_circuit is True for item in semantic_analysis_cases)
        / len(semantic_analysis_cases)
        if semantic_analysis_cases else None
    )
    semantic_analysis_completion_rate = (
        sum(item.semantic_analysis_complete is True for item in semantic_analysis_cases)
        / len(semantic_analysis_cases)
        if semantic_analysis_cases else None
    )
    citation_validity = (
        sum(
            item.candidate_citation_ids_valid and item.candidate_has_required_citation
            for item in repair_cases
        ) / len(repair_cases)
        if repair_cases else 0.0
    )
    behavior_invariants = all(
        item.served_answer_source == "legacy_baseline"
        and item.shadow_additional_llm_calls == 0
        and item.shadow_synchronous_db_writes == 0
        and item.user_visible_behavior_changed is False
        and item.business_persistence_changed is False
        for item in qualifying
    )

    reviewed_by_id = {
        item.case_id: item
        for item in (reviews.reviews if reviews is not None else [])
        if item.status == ReviewStatus.REVIEWED
    }
    reviewed_repairs = [
        reviewed_by_id[item.case_id]
        for item in repair_cases
        if item.case_id in reviewed_by_id
    ]
    reviewed_eligible = [
        reviewed_by_id[item.case_id]
        for item in eligible
        if item.case_id in reviewed_by_id
    ]
    review_coverage = len(reviewed_repairs) / len(repair_cases) if repair_cases else 0.0
    eligible_review_coverage = (
        len(reviewed_eligible) / len(eligible) if eligible else 0.0
    )
    score_differences = [
        float(item.candidate_score) - float(item.baseline_score)
        for item in reviewed_repairs
        if item.candidate_score is not None and item.baseline_score is not None
    ]
    candidate_mean = (
        statistics.fmean(float(item.candidate_score) for item in reviewed_repairs)
        if reviewed_repairs else None
    )
    baseline_mean = (
        statistics.fmean(float(item.baseline_score) for item in reviewed_repairs)
        if reviewed_repairs else None
    )
    mean_difference = (
        candidate_mean - baseline_mean
        if candidate_mean is not None and baseline_mean is not None
        else None
    )
    candidate_pass_rate = (
        sum(item.candidate_pass is True for item in reviewed_repairs) / len(reviewed_repairs)
        if reviewed_repairs else None
    )
    baseline_pass_rate = (
        sum(item.baseline_pass is True for item in reviewed_repairs) / len(reviewed_repairs)
        if reviewed_repairs else None
    )
    pass_rate_difference = (
        candidate_pass_rate - baseline_pass_rate
        if candidate_pass_rate is not None and baseline_pass_rate is not None
        else None
    )
    bootstrap_lower = _bootstrap_lower_bound(score_differences)
    safety_issue_codes = {
        "unsupported_claim", "scope_error", "as_of_error", "critical_claim_error"
    }
    all_reviewed_new_issue_counts = Counter(
        issue
        for item in reviewed_repairs
        for issue in _new_candidate_issues(item)
        if issue in safety_issue_codes
    )
    eligible_new_issue_counts = Counter(
        issue
        for item in reviewed_eligible
        for issue in _new_candidate_issues(item)
        if issue in safety_issue_codes
    )

    metrics: dict[str, Any] = {
        "qualifying_case_count": len(qualifying),
        "data_complete_repair_case_count": len(repair_cases),
        "no_repair_control_count": len(controls),
        "distinct_request_hash_count": len(request_hashes),
        "projected_skip_case_count": len(eligible),
        "projected_repair_call_reduction": repair_reduction,
        "paired_median_completion_improvement_ms": median_savings,
        "shadow_compute_p95_ms": shadow_p95,
        "shadow_source_support_compute_p95_ms": source_support_p95,
        "shadow_decision_compute_p95_ms": decision_p95,
        "semantic_profile_case_count": len(semantic_profile_cases),
        "semantic_analysis_case_count": len(semantic_analysis_cases),
        "semantic_analysis_rate": semantic_analysis_rate,
        "semantic_support_materialized_count_p95": materialized_source_p95,
        "semantic_short_circuit_rate": semantic_short_circuit_rate,
        "semantic_analysis_completion_rate": semantic_analysis_completion_rate,
        "candidate_citation_validity_rate": citation_validity,
        "promotable_error_signatures": signatures,
        "real_case_count_by_promotable_signature": dict(sorted(real_signature_counts.items())),
        "behavior_invariants_preserved": behavior_invariants,
        "reviewed_repair_case_count": len(reviewed_repairs),
        "review_coverage": review_coverage,
        "reviewed_eligible_case_count": len(reviewed_eligible),
        "eligible_review_coverage": eligible_review_coverage,
        "candidate_mean_score": candidate_mean,
        "baseline_mean_score": baseline_mean,
        "mean_score_difference": mean_difference,
        "candidate_pass_rate": candidate_pass_rate,
        "baseline_pass_rate": baseline_pass_rate,
        "pass_rate_difference": pass_rate_difference,
        "bootstrap_95_lower_score_difference": bootstrap_lower,
        "new_candidate_issue_counts_all_reviewed": dict(
            sorted(all_reviewed_new_issue_counts.items())
        ),
        "new_candidate_issue_counts_eligible": dict(
            sorted(eligible_new_issue_counts.items())
        ),
        "new_candidate_issue_counts": dict(sorted(eligible_new_issue_counts.items())),
        "incomplete_case_count": sum(not item.data_complete for item in cohort.observations),
        "synthetic_case_count_excluded": sum(
            item.origin == EvidenceOrigin.SYNTHETIC for item in cohort.observations
        ),
    }
    gates = {
        "minimum_30_repair_cases": len(repair_cases) >= 30,
        "minimum_10_no_repair_controls": len(controls) >= 10,
        "minimum_15_distinct_requests": len(request_hashes) >= 15,
        "minimum_5_real_cases_per_promotable_signature": bool(signatures)
        and all(real_signature_counts[signature] >= 5 for signature in signatures),
        "citation_id_validity_100_percent": citation_validity == 1.0,
        "projected_repair_call_reduction_at_least_30_percent": repair_reduction >= 0.30,
        "paired_median_completion_improvement_at_least_5_seconds": median_savings >= 5000.0,
        "shadow_compute_p95_below_5ms": shadow_p95 is not None and shadow_p95 < 5.0,
        "zero_additional_llm_db_or_behavior_change": behavior_invariants,
        "all_repair_cases_reviewed": bool(repair_cases) and review_coverage == 1.0,
        "candidate_mean_noninferior_within_3_points": mean_difference is not None
        and mean_difference >= -3.0,
        "candidate_pass_rate_noninferior_within_5pp": pass_rate_difference is not None
        and pass_rate_difference >= -0.05,
        "bootstrap_95_lower_bound_at_least_minus_5": bootstrap_lower is not None
        and bootstrap_lower >= -5.0,
        "no_new_unsupported_scope_asof_or_critical_errors": not eligible_new_issue_counts,
    }
    report = GroundingRepairGateReport(
        cohort_id=cohort.cohort_id,
        cohort_hash=cohort.content_hash,
        review_hash=reviews.content_hash if reviews is not None else None,
        generated_at=datetime.now(timezone.utc),
        metrics=metrics,
        gates=gates,
        all_passed=all(gates.values()),
    )
    return stamp_content_hash(report)


def render_grounding_repair_markdown(report: GroundingRepairGateReport) -> str:
    lines = [
        "# Grounding Repair Shadow Evidence Report",
        "",
        f"- Cohort: `{report.cohort_id}`",
        f"- Generated: `{report.generated_at.isoformat()}`",
        f"- Overall: **{'PASS' if report.all_passed else 'NOT READY'}**",
        "",
        "## Gates",
        "",
    ]
    for name, passed in report.gates.items():
        lines.append(f"- {'PASS' if passed else 'FAIL'} `{name}`")
    lines.extend(["", "## Metrics", ""])
    for name, value in report.metrics.items():
        rendered = json.dumps(value, ensure_ascii=False, sort_keys=True)
        lines.append(f"- `{name}`: {rendered}")
    lines.extend([
        "",
        "## Decision boundary",
        "",
        "This report evaluates shadow/replay evidence only. It does not authorize selective serving or a user-visible canary.",
    ])
    return "\n".join(lines).rstrip() + "\n"


def write_grounding_repair_report(
    report: GroundingRepairGateReport,
    json_path: Path,
    markdown_path: Path | None = None,
) -> tuple[Path, Path]:
    json_destination = Path(json_path).resolve()
    markdown_destination = (
        Path(markdown_path).resolve()
        if markdown_path is not None
        else json_destination.with_suffix(".md")
    )
    write_model(json_destination, report)
    markdown_destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = markdown_destination.with_suffix(markdown_destination.suffix + ".tmp")
    temporary.write_text(render_grounding_repair_markdown(report), encoding="utf-8")
    temporary.replace(markdown_destination)
    return json_destination, markdown_destination


__all__ = [
    "EvidenceOrigin",
    "GroundingRepairCaseReview",
    "GroundingRepairGateReport",
    "GroundingRepairReviewBatch",
    "GroundingRepairSelectionItem",
    "GroundingRepairSelectionManifest",
    "GroundingRepairShadowCohort",
    "GroundingRepairShadowObservation",
    "ReviewStatus",
    "build_grounding_repair_review_template",
    "collect_grounding_repair_shadow_cohort",
    "combine_grounding_repair_cohorts",
    "evaluate_grounding_repair_gates",
    "render_grounding_repair_markdown",
    "seal_grounding_repair_reviews",
    "verify_grounding_repair_cohort_content_hash",
    "write_grounding_repair_report",
]
