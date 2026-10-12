"""Aggregate-only post-hoc development runner for grounding-repair Policy-v7.

The runner reuses the sealed Policy-v4 R6 holdout solely as development evidence.
Decrypted payloads and canonical answers remain in memory. Durable output contains
aggregate counts, timings, gate results, and artifact-level lineage only.
"""

from __future__ import annotations

from collections import Counter
from datetime import datetime
from enum import Enum
from pathlib import Path
from typing import Any, Literal

from pydantic import Field, model_validator

from src.assistant.evals.artifacts import read_model, write_model
from src.assistant.evals.grounding_repair_candidate_v7 import POLICY_VERSION_V7
from src.assistant.evals.grounding_repair_v4_protocol import (
    AutomatedReviewBatch,
    HistoricalExclusionManifest,
    HoldoutCase,
    HoldoutCollection,
    POLICY_V4,
    Preregistration,
    QuestionManifest,
    SelectionManifest,
    V4Model,
    stamp_v4,
    verify_v4,
)
from src.assistant.evals.grounding_repair_v4_replay import (
    ReplayCache,
    prime_replay_cache,
)


class DevelopmentStatusV7(str, Enum):
    """Outcome of an offline post-hoc Policy-v7 development pass."""

    PASSED = "posthoc_development_gates_passed_not_confirmation"
    FAILED_SAFETY = "posthoc_development_failed_safety"
    INCONCLUSIVE = "posthoc_development_inconclusive_or_utility_failed"
    INCOMPLETE = "posthoc_development_incomplete"


class DevelopmentReportV7(V4Model):
    """Plaintext-free, aggregate-only Policy-v7 development evidence."""

    schema_version: Literal[1] = 1
    experiment_id: str = Field(min_length=3)
    generated_at: datetime
    status: DevelopmentStatusV7
    policy_version: Literal[POLICY_VERSION_V7] = POLICY_VERSION_V7
    source_policy_version: Literal[POLICY_V4] = POLICY_V4
    source_preregistration_hash: str = Field(min_length=8)
    source_question_manifest_hash: str = Field(min_length=8)
    source_historical_exclusion_hash: str = Field(min_length=8)
    source_holdout_hash: str = Field(min_length=8)
    source_selection_hash: str = Field(min_length=8)
    source_review_hash: str = Field(min_length=8)
    transformed_review_hash: str | None = Field(default=None, min_length=8)
    transformed_review_status: Literal[
        "evaluated", "unavailable", "incomplete", "invalid"
    ] = "unavailable"
    metrics: dict[str, int | float | bool | None]
    gates: dict[str, bool]
    decision_reason_counts: dict[str, int]
    risk_code_counts: dict[str, int]
    source_row_count_bands: dict[str, int]
    emitted_row_count_bands: dict[str, int]
    posthoc: Literal[True] = True
    confirmation_evidence: Literal[False] = False
    research_only: Literal[True] = True
    serving_change_authorized: Literal[False] = False
    content_hash: str = ""

    @model_validator(mode="after")
    def validate_aggregate_contract(self) -> "DevelopmentReportV7":
        count_maps = (
            self.decision_reason_counts,
            self.risk_code_counts,
            self.source_row_count_bands,
            self.emitted_row_count_bands,
        )
        if any(value < 0 for mapping in count_maps for value in mapping.values()):
            raise ValueError("aggregate counts cannot be negative")
        return self



from src.assistant.evals.grounding_repair_v7_development_support import (
    PrimedCandidateV7,
    _band,
    _case_payload,  # Compatibility patch point retained for validation tests.
    _evaluate_transformed_reviews,
    _lineage_valid,
    _p95,
    _replay_case_v7,
    _safe_code,
    _utcnow,
    _validated_inputs,
)

def derive_certificate_selected_work_v7(
    *,
    historical: HistoricalExclusionManifest,
    manifest: QuestionManifest,
    preregistration: Preregistration,
    holdout: HoldoutCollection,
    selection: SelectionManifest,
    reviews: AutomatedReviewBatch,
) -> list[tuple[str, str]]:
    """Return the deterministic certificate-selected worklist in memory only."""

    artifacts = (historical, manifest, preregistration, holdout, selection, reviews)
    if not all(verify_v4(item) for item in artifacts):
        raise ValueError("Policy-v7 worklist source artifact hash is invalid")
    if not _lineage_valid(
        historical=historical,
        manifest=manifest,
        preregistration=preregistration,
        holdout=holdout,
        selection=selection,
        reviews=reviews,
    ):
        raise ValueError("Policy-v7 worklist source lineage is invalid")
    prepared = _validated_inputs(
        holdout=holdout, selection=selection, reviews=reviews
    )
    if len(prepared) != selection.selected_case_count:
        raise ValueError("Policy-v7 worklist encrypted payload is invalid")
    replay_cache = ReplayCache()
    prime_replay_cache([payload for _case, payload, _review, _primed in prepared], replay_cache)
    selected: list[tuple[str, str]] = []
    for case, payload, _review, primed in prepared:
        replay, decision, _profile = _replay_case_v7(payload, replay_cache, primed)
        if not all(replay[name] for name in (
            "candidate_parity", "initial_error_parity", "candidate_error_parity"
        )):
            raise ValueError("Policy-v7 worklist replay parity failed")
        if decision.would_skip_repair:
            if case.case_payload_hash is None:
                raise ValueError("Policy-v7 selected case payload hash is missing")
            selected.append((case.question_hash, case.case_payload_hash))
    return selected

def run_development_v7(
    *,
    historical: HistoricalExclusionManifest,
    manifest: QuestionManifest,
    preregistration: Preregistration,
    holdout: HoldoutCollection,
    selection: SelectionManifest,
    reviews: AutomatedReviewBatch,
    transformed_reviews: Any | None = None,
    output_path: Path | None = None,
    generated_at: datetime | None = None,
) -> DevelopmentReportV7:
    """Run aggregate-only Policy-v7 diagnostics against a sealed holdout."""

    artifacts_valid = all(
        verify_v4(item)
        for item in (historical, manifest, preregistration, holdout, selection, reviews)
    )
    lineage_valid = _lineage_valid(
        historical=historical,
        manifest=manifest,
        preregistration=preregistration,
        holdout=holdout,
        selection=selection,
        reviews=reviews,
    )
    candidate_prime_times: list[float] = []
    prepared = []
    if artifacts_valid and lineage_valid:
        prepared = _validated_inputs(
            holdout=holdout,
            selection=selection,
            reviews=reviews,
            candidate_prime_times=candidate_prime_times,
        )

    payloads = [payload for _case, payload, _review, _primed in prepared]
    if prepared:
        warm_cache = ReplayCache()
        prime_replay_cache([prepared[0][1]], warm_cache)
        try:
            _replay_case_v7(prepared[0][1], warm_cache, prepared[0][3])
        except Exception:
            pass

    replay_cache = ReplayCache()
    prime_replay_cache(payloads, replay_cache)
    replay_count = candidate_parity = initial_parity = candidate_error_parity = 0
    selected: list[tuple[HoldoutCase, PrimedCandidateV7]] = []
    certificate_times: list[float] = []
    full_times: list[float] = []
    builder_times: list[float] = []
    decision_times: list[float] = []
    decision_reasons: Counter[str] = Counter()
    risk_codes: Counter[str] = Counter()
    source_bands: Counter[str] = Counter()
    emitted_bands: Counter[str] = Counter()

    for case, payload, _review, primed in prepared:
        try:
            replay, decision, profile = _replay_case_v7(payload, replay_cache, primed)
        except Exception:
            decision_reasons["replay_exception"] += 1
            continue
        replay_count += 1
        candidate_parity += int(replay["candidate_parity"])
        initial_parity += int(replay["initial_error_parity"])
        candidate_error_parity += int(replay["candidate_error_parity"])
        certificate_times.append(float(replay["certificate_ms"]))
        full_times.append(float(replay["full_replay_ms"]))
        builder_times.append(profile.candidate_build_compute_ms)
        decision_times.append(profile.decision_compute_ms)
        decision_reasons[_safe_code(decision.reason)] += 1
        risk_codes.update(_safe_code(code) for code in decision.risk_codes)
        source_bands[_band(decision.source_row_count, cuts=(0, 10, 25, 50, 100))] += 1
        emitted_bands[_band(decision.emitted_row_count, cuts=(0, 2, 5, 10, 20))] += 1
        if decision.would_skip_repair:
            selected.append((case, primed))

    target = preregistration.gates.repair_case_target
    selected_cases = [item[0] for item in prepared]
    complete_repair_count = sum(
        int(item.complete and item.repair_triggered) for item in selected_cases
    )
    distinct_request_count = len({item.request_hash for item in selected_cases})
    complete_reviews = sum(int(item[2].complete) for item in prepared)
    review_coverage = complete_reviews / target if target else 0.0
    selected_count = len(selected)
    projected_reduction = selected_count / target if target else 0.0
    replay_parity = (
        replay_count == target
        and candidate_parity == target
        and initial_parity == target
        and candidate_error_parity == target
    )

    transformed_hash = None
    transformed_status = "unavailable"
    transformed_lineage = False
    transformed_records = False
    transformed_complete = 0
    transformed_coverage = 0.0
    false_positives = None
    selected_issue_count = None
    if transformed_reviews is not None:
        transformed_hash = str(getattr(transformed_reviews, "content_hash", "") or "") or None
        try:
            (
                transformed_status,
                transformed_lineage,
                transformed_records,
                transformed_complete,
                transformed_coverage,
                false_positives,
                selected_issue_count,
            ) = _evaluate_transformed_reviews(
                transformed_reviews=transformed_reviews,
                selected=selected,
                holdout=holdout,
                selection=selection,
                reviews=reviews,
                preregistration=preregistration,
            )
        except Exception:
            transformed_status = "invalid"

    certificate_p95 = _p95(certificate_times)
    full_replay_p95 = _p95(full_times)
    gates = {
        "input_artifacts_valid": artifacts_valid,
        "lineage_valid": lineage_valid,
        "selection_complete": selection.status == "complete" and selection.selected_case_count == target,
        "complete_repair_case_target_met": complete_repair_count == target,
        "distinct_request_target_met": distinct_request_count == preregistration.gates.distinct_request_target,
        "automated_review_coverage_met": review_coverage >= preregistration.gates.automated_review_coverage,
        "replay_candidate_and_error_parity": replay_parity,
        "minimum_certificate_selected_cases_met": selected_count >= preregistration.gates.minimum_selected_cases,
        "minimum_projected_repair_reduction_met": projected_reduction >= preregistration.gates.minimum_projected_repair_reduction,
        "certificate_p95_met": certificate_p95 < preregistration.gates.maximum_certificate_p95_ms,
        "full_replay_p95_met": full_replay_p95 < preregistration.gates.maximum_full_replay_p95_ms,
        "transformed_review_lineage_valid": transformed_lineage,
        "transformed_review_records_valid": transformed_records,
        "transformed_review_coverage_met": transformed_status == "evaluated" and transformed_coverage >= preregistration.gates.automated_review_coverage,
        "false_positive_limit_met": false_positives is not None and false_positives <= preregistration.gates.maximum_false_positives,
        "selected_candidate_issue_limit_met": selected_issue_count is not None and selected_issue_count <= preregistration.gates.maximum_selected_candidate_issue_count,
    }
    core_names = (
        "input_artifacts_valid", "lineage_valid", "selection_complete",
        "complete_repair_case_target_met", "distinct_request_target_met",
        "automated_review_coverage_met", "replay_candidate_and_error_parity",
    )
    safety_names = ("false_positive_limit_met", "selected_candidate_issue_limit_met")
    if not all(gates[name] for name in core_names):
        status = DevelopmentStatusV7.INCOMPLETE
    elif transformed_status in {"invalid", "incomplete"}:
        status = DevelopmentStatusV7.INCOMPLETE
    elif transformed_status == "evaluated" and not all(gates[name] for name in safety_names):
        status = DevelopmentStatusV7.FAILED_SAFETY
    elif not all(gates.values()):
        status = DevelopmentStatusV7.INCONCLUSIVE
    else:
        status = DevelopmentStatusV7.PASSED

    metrics: dict[str, int | float | bool | None] = {
        "holdout_case_count": len(holdout.cases),
        "complete_repair_case_count": complete_repair_count,
        "selected_case_count": selection.selected_case_count,
        "distinct_request_count": distinct_request_count,
        "automated_review_complete_count": complete_reviews,
        "automated_review_coverage": review_coverage,
        "decrypted_selected_case_count": len(prepared),
        "replay_complete_count": replay_count,
        "stored_candidate_parity_count": candidate_parity,
        "initial_error_parity_count": initial_parity,
        "candidate_error_parity_count": candidate_error_parity,
        "certificate_selected_count": selected_count,
        "projected_repair_reduction": projected_reduction,
        "canonical_builder_prime_p95_ms": _p95(candidate_prime_times),
        "canonical_builder_p95_ms": _p95(builder_times),
        "certificate_decision_p95_ms": _p95(decision_times),
        "certificate_p95_ms": certificate_p95,
        "full_replay_p95_ms": full_replay_p95,
        "transformed_review_complete_count": transformed_complete,
        "transformed_review_coverage": transformed_coverage,
        "false_positive_count": false_positives,
        "selected_candidate_issue_count": selected_issue_count,
    }
    report = stamp_v4(DevelopmentReportV7(
        experiment_id=preregistration.experiment_id,
        generated_at=generated_at or _utcnow(),
        status=status,
        source_preregistration_hash=preregistration.content_hash,
        source_question_manifest_hash=manifest.content_hash,
        source_historical_exclusion_hash=historical.content_hash,
        source_holdout_hash=holdout.content_hash,
        source_selection_hash=selection.content_hash,
        source_review_hash=reviews.content_hash,
        transformed_review_hash=transformed_hash,
        transformed_review_status=transformed_status,
        metrics=metrics,
        gates=gates,
        decision_reason_counts=dict(sorted(decision_reasons.items())),
        risk_code_counts=dict(sorted(risk_codes.items())),
        source_row_count_bands=dict(sorted(source_bands.items())),
        emitted_row_count_bands=dict(sorted(emitted_bands.items())),
    ))
    if output_path is not None:
        write_model(Path(output_path), report)
    return report


def _read_transformed_reviews_v7(path: Path) -> Any:
    from src.assistant.evals.grounding_repair_v7_review import TransformedReviewArtifactV7

    return read_model(Path(path), TransformedReviewArtifactV7)


def run_development_from_paths_v7(
    *,
    artifact_dir: Path,
    output_path: Path,
    transformed_reviews_path: Path | None = None,
    generated_at: datetime | None = None,
) -> DevelopmentReportV7:
    root = Path(artifact_dir)
    return run_development_v7(
        historical=read_model(root / "historical-exclusions.json", HistoricalExclusionManifest),
        manifest=read_model(root / "question-manifest.json", QuestionManifest),
        preregistration=read_model(root / "preregistration.json", Preregistration),
        holdout=read_model(root / "holdout.json", HoldoutCollection),
        selection=read_model(root / "selection.json", SelectionManifest),
        reviews=read_model(root / "reviews.json", AutomatedReviewBatch),
        transformed_reviews=(
            _read_transformed_reviews_v7(transformed_reviews_path)
            if transformed_reviews_path is not None and Path(transformed_reviews_path).exists()
            else None
        ),
        output_path=output_path,
        generated_at=generated_at,
    )


__all__ = [
    "DevelopmentReportV7",
    "_case_payload",  # Compatibility patch point; payloads remain in memory only.
    "DevelopmentStatusV7",
    "derive_certificate_selected_work_v7",
    "run_development_from_paths_v7",
    "run_development_v7",
]
