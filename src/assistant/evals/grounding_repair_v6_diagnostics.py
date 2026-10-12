"""Aggregate-only safety diagnostics for offline grounding-repair Policy-v6.

Sensitive holdout payloads and transformed review rows are decrypted only in memory.
The returned artifact contains cohort-level counters and artifact lineage; it never
contains per-case identifiers, questions, answers, sources, rationales, or hashes.
"""

from __future__ import annotations

import re
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Literal

from pydantic import Field, model_validator

from src.assistant.evals.artifacts import content_hash, read_model, write_model
from src.assistant.evals.grounding_repair_candidate_v6 import (
    POLICY_VERSION_V6,
    answer_contract_v6,
    filter_grounding_repair_candidate_v6,
)
from src.assistant.evals.grounding_repair_certificate_v5_projection import (
    ProjectionCacheV5,
)
from src.assistant.evals.grounding_repair_certificate_v5_rules import (
    CITATION_RE,
    LineContractCacheV5,
    classify_line_v5,
)
from src.assistant.evals.grounding_repair_v4_protocol import (
    AutomatedCaseReview,
    AutomatedReviewBatch,
    HoldoutCollection,
    Preregistration,
    SelectionManifest,
    V4Model,
    verify_v4,
)
from src.assistant.evals.grounding_repair_v6_development import _case_payload, _oracle_safe
from src.assistant.evals.grounding_repair_v6_review import (
    TransformedReviewArtifactV6,
    decrypt_transformed_reviews_v6,
    transform_implementation_hash_v6,
    transformed_candidate_hash_v6,
    transformed_case_payload_hash_v6,
    transformed_prompt_identities_v6,
    transformed_review_identity_v6,
)

_SAFE_CODE_RE = re.compile(r"^[A-Za-z0-9_.:+-]+$")
_BANDS = (0, 1, 3, 6, 11, 21, 41, 81)
_FORBIDDEN_AGGREGATE_NAMES = {
    "question",
    "answer",
    "candidate_answer",
    "baseline_answer",
    "sources",
    "reference",
    "rationale",
    "records",
    "question_hash",
    "request_hash",
    "case_payload_hash",
    "run_id",
    "turn_id",
    "trace_id",
    "event_id",
}


class SafetyFailureDiagnosticV6(V4Model):
    """Plaintext-free aggregate diagnosis of the Policy-v6 safety-gate failure."""

    schema_version: Literal[1] = 1
    experiment_id: str = Field(min_length=3)
    generated_at: datetime
    policy_version: Literal[POLICY_VERSION_V6] = POLICY_VERSION_V6
    source_holdout_hash: str = Field(min_length=8)
    source_selection_hash: str = Field(min_length=8)
    source_review_hash: str = Field(min_length=8)
    transformed_review_hash: str = Field(min_length=8)
    reviewed_case_count: int = Field(ge=0)
    complete_review_count: int = Field(ge=0)
    issue_case_count: int = Field(ge=0)
    candidate_issue_count: int = Field(ge=0)
    aggregate_counts: dict[str, dict[str, int]]
    privacy: dict[str, bool]
    research_only: Literal[True] = True
    serving_change_authorized: Literal[False] = False
    content_hash: str = ""

    @model_validator(mode="after")
    def validate_aggregate_only(self) -> "SafetyFailureDiagnosticV6":
        for family, counts in self.aggregate_counts.items():
            normalized_family = family.casefold()
            if normalized_family in _FORBIDDEN_AGGREGATE_NAMES or normalized_family.endswith("_hash"):
                raise ValueError("diagnostic family exposes a forbidden payload field")
            if any(value < 0 for value in counts.values()):
                raise ValueError("diagnostic aggregate counts cannot be negative")
            for key in counts:
                normalized_key = str(key).casefold()
                if (
                    not _SAFE_CODE_RE.fullmatch(str(key))
                    or normalized_key in _FORBIDDEN_AGGREGATE_NAMES
                    or normalized_key.startswith("sha256:")
                ):
                    raise ValueError("diagnostic aggregate key is not a safe code")
        return self


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _safe_code(value: str | None) -> str:
    raw = str(value or "none").strip().replace(" ", "_")
    return raw if _SAFE_CODE_RE.fullmatch(raw) else "unsafe_code"


def _combo(values: Iterable[str], *, empty: str = "none") -> str:
    codes = sorted({_safe_code(value) for value in values if str(value or "").strip()})
    return "+".join(codes) if codes else empty


def _band(value: int) -> str:
    number = max(0, int(value))
    if number == 0:
        return "n0"
    for lower, upper in zip(_BANDS[1:], _BANDS[2:], strict=False):
        if number < upper:
            return f"n{lower}_{upper - 1}"
    return f"n{_BANDS[-1]}_plus"


def _ratio_band(numerator: int, denominator: int) -> str:
    if denominator <= 0:
        return "none"
    ratio = numerator / denominator
    if ratio >= 1.0:
        return "r100"
    if ratio >= 0.95:
        return "r95_99"
    if ratio >= 0.90:
        return "r90_94"
    if ratio >= 0.80:
        return "r80_89"
    if ratio >= 0.60:
        return "r60_79"
    return "r0_59"


def _score_band(value: float | None) -> str:
    if value is None:
        return "missing"
    score = float(value)
    if score >= 95:
        return "s95_100"
    if score >= 90:
        return "s90_94"
    if score >= 80:
        return "s80_89"
    if score >= 70:
        return "s70_79"
    return "s0_69"


def _line_citation_role(line: str, projections: dict[str, Any]) -> str:
    cited = [
        projections[source_id]
        for source_id in CITATION_RE.findall(line)
        if source_id in projections
    ]
    if not cited:
        return "uncited"
    has_summary = any(item.is_query_summary for item in cited)
    has_direct = any(not item.is_query_summary for item in cited)
    if has_summary and has_direct:
        return "mixed"
    return "summary_only" if has_summary else "direct_only"


def _citation_role_combination(citation_roles: Counter[str]) -> str:
    roles: list[str] = []
    if citation_roles.get("summary_only"):
        roles.append("summary")
    if citation_roles.get("direct_only"):
        roles.append("direct")
    if citation_roles.get("mixed"):
        roles.append("mixed_line")
    return _combo(roles, empty="no_cited_fact")


def _review_outcome(review: AutomatedCaseReview) -> str:
    if not review.complete:
        return "incomplete"
    return "unsafe" if review.candidate_issue_codes else "safe"


def _validate_lineage(
    *,
    holdout: HoldoutCollection,
    selection: SelectionManifest,
    source_reviews: AutomatedReviewBatch,
    transformed_reviews: TransformedReviewArtifactV6,
    preregistration: Preregistration,
) -> None:
    if not all(
        verify_v4(item)
        for item in (holdout, selection, source_reviews, transformed_reviews, preregistration)
    ):
        raise ValueError("Policy-v6 diagnostic source artifact hash is invalid")
    reference_hashes, judge_hash = transformed_prompt_identities_v6()
    expected = (
        selection.holdout_hash,
        source_reviews.selection_hash,
        holdout.preregistration_hash,
        transformed_reviews.source_holdout_hash,
        transformed_reviews.source_selection_hash,
        transformed_reviews.source_review_hash,
        transformed_reviews.source_model_identity_hash,
        transformed_reviews.transformed_model_identity_hash,
        transformed_reviews.transformed_reference_prompt_identity_hashes,
        transformed_reviews.transformed_judge_prompt_identity_hash,
        transformed_reviews.transform_implementation_hash,
        transformed_reviews.status,
        transformed_reviews.review_count,
        transformed_reviews.review_target_count,
        transformed_reviews.source_selection_case_count,
        transformed_reviews.experiment_id,
    )
    actual = (
        holdout.content_hash,
        selection.content_hash,
        preregistration.content_hash,
        holdout.content_hash,
        selection.content_hash,
        source_reviews.content_hash,
        preregistration.model_identity_hash,
        transformed_review_identity_v6(preregistration),
        reference_hashes,
        judge_hash,
        transform_implementation_hash_v6(),
        "complete",
        transformed_reviews.review_target_count,
        transformed_reviews.review_target_count,
        selection.selected_case_count,
        selection.experiment_id,
    )
    if expected != actual:
        raise ValueError("Policy-v6 diagnostic source lineage is invalid")


def analyze_policy_v6_safety_failure(
    *,
    holdout: HoldoutCollection,
    selection: SelectionManifest,
    source_reviews: AutomatedReviewBatch,
    transformed_reviews: TransformedReviewArtifactV6,
    preregistration: Preregistration,
    generated_at: datetime | None = None,
) -> SafetyFailureDiagnosticV6:
    """Build aggregate failure clusters while keeping all case payloads in memory."""

    _validate_lineage(
        holdout=holdout,
        selection=selection,
        source_reviews=source_reviews,
        transformed_reviews=transformed_reviews,
        preregistration=preregistration,
    )
    records = decrypt_transformed_reviews_v6(transformed_reviews)
    if len(records) != transformed_reviews.review_target_count:
        raise ValueError("Policy-v6 diagnostic transformed review coverage is incomplete")

    cases = {item.question_hash: item for item in holdout.cases}
    source_review_by_hash = {item.question_hash: item for item in source_reviews.reviews}
    counters: dict[str, Counter[str]] = {
        "issue_code_combinations": Counter(),
        "individual_issue_codes": Counter(),
        "tool_combinations": Counter(),
        "query_scope_combinations": Counter(),
        "source_v4_safety": Counter(),
        "source_v4_to_v6_safety": Counter(),
        "original_line_count_bands": Counter(),
        "transformed_line_count_bands": Counter(),
        "source_factual_line_count_bands": Counter(),
        "transformed_factual_line_count_bands": Counter(),
        "structural_line_count_bands": Counter(),
        "section_count_bands": Counter(),
        "retained_section_count_bands": Counter(),
        "transformed_factual_retention_bands": Counter(),
        "query_summary_cited_line_count_bands": Counter(),
        "direct_source_cited_line_count_bands": Counter(),
        "mixed_citation_line_count_bands": Counter(),
        "citation_role_combinations": Counter(),
        "filter_drop_reason_combinations": Counter(),
        "filter_unsupported_drop_count_bands": Counter(),
        "filter_summary_drop_count_bands": Counter(),
        "filter_scope_mismatch_drop_count_bands": Counter(),
        "filter_mixed_citation_drop_count_bands": Counter(),
        "filter_dropped_section_count_bands": Counter(),
        "filter_unknown_citation_drop_count_bands": Counter(),
        "filter_internal_drop_count_bands": Counter(),
        "filter_orphan_structure_drop_count_bands": Counter(),
        "answer_contract_decisions": Counter(),
        "answer_contract_reasons": Counter(),
        "omission_notice_count_bands": Counter(),
        "selected_candidate_score_bands": Counter(),
        "selected_preferred_answer": Counter(),
        "review_outcome_by_query_scope": Counter(),
        "review_outcome_by_citation_role": Counter(),
        "review_outcome_by_retention_band": Counter(),
        "review_outcome_by_section_count_band": Counter(),
        "review_outcome_by_answer_contract_reason": Counter(),
        "review_outcome_by_filter_drop_reason": Counter(),
    }
    complete_count = issue_case_count = candidate_issue_count = 0
    contract_cache = LineContractCacheV5()
    seen_records: set[tuple[str, str]] = set()

    for record in records:
        identity = (record.source_question_hash, record.source_case_payload_hash)
        if identity in seen_records:
            raise ValueError("Policy-v6 diagnostic transformed records contain duplicates")
        seen_records.add(identity)
        case = cases.get(record.source_question_hash)
        source_review = source_review_by_hash.get(record.source_question_hash)
        if case is None or source_review is None or case.case_payload_hash is None:
            raise ValueError("Policy-v6 diagnostic case lineage is incomplete")
        payload = _case_payload(case)
        if payload is None:
            raise ValueError("Policy-v6 diagnostic encrypted payload is invalid")
        sources = [item for item in payload.get("sources") or [] if isinstance(item, dict)]
        tool_results = [
            item for item in payload.get("tool_results") or [] if isinstance(item, dict)
        ]
        cache = ProjectionCacheV5.build(sources, tool_results)
        filtered = filter_grounding_repair_candidate_v6(
            candidate_answer=str(payload.get("candidate_answer") or ""),
            question=str(payload.get("question") or ""),
            sources=sources,
            tool_results=tool_results,
            projection_cache=cache,
            contract_cache=contract_cache,
        )
        candidate_hash = transformed_candidate_hash_v6(filtered.candidate_answer)
        payload_hash = transformed_case_payload_hash_v6(
            source_case_payload_hash=case.case_payload_hash,
            transformed_candidate_hash=candidate_hash,
        )
        if (
            record.source_case_payload_hash != case.case_payload_hash
            or record.transformed_candidate_hash != candidate_hash
            or record.transformed_case_payload_hash != payload_hash
            or record.review.case_payload_hash != payload_hash
            or record.review.model_identity_hash
            != transformed_reviews.transformed_model_identity_hash
        ):
            raise ValueError(
                "Policy-v6 diagnostic transformed record does not match implementation"
            )

        review = record.review
        outcome = _review_outcome(review)
        if review.complete:
            complete_count += 1
        issue_codes = [_safe_code(code) for code in review.candidate_issue_codes]
        if issue_codes:
            issue_case_count += 1
            candidate_issue_count += len(issue_codes)
        counters["issue_code_combinations"][_combo(issue_codes)] += 1
        counters["individual_issue_codes"].update(issue_codes)
        tools = [
            _safe_code(item.get("tool"))
            for item in tool_results
            if item.get("tool") != "think"
        ]
        counters["tool_combinations"][_combo(tools)] += 1

        transformed_lines = filtered.candidate_answer.splitlines()
        cited_ids = set(CITATION_RE.findall(filtered.candidate_answer))
        projections = cache.materialize(cited_ids)
        scopes = {item.query_scope for item in projections.values() if item.query_scope}
        scope_combo = _combo(scopes)
        counters["query_scope_combinations"][scope_combo] += 1
        counters["review_outcome_by_query_scope"][f"{outcome}+{scope_combo}"] += 1

        source_safe = _oracle_safe(source_review, preregistration)
        transformed_safe = bool(review.complete and not review.candidate_issue_codes)
        counters["source_v4_safety"]["safe" if source_safe else "unsafe"] += 1
        transition = (
            f"source_{'safe' if source_safe else 'unsafe'}_to_"
            f"{'safe' if transformed_safe else 'unsafe'}"
        )
        counters["source_v4_to_v6_safety"][transition] += 1

        original_lines = str(payload.get("candidate_answer") or "").splitlines()
        source_factual_count = sum(
            classify_line_v5(line.strip()) == "factual" for line in original_lines
        )
        transformed_factual_count = sum(
            classify_line_v5(line.strip()) == "factual" for line in transformed_lines
        )
        structural_count = sum(
            classify_line_v5(line.strip()) == "structural" for line in transformed_lines
        )
        retention_band = _ratio_band(transformed_factual_count, source_factual_count)
        counters["original_line_count_bands"][_band(len(original_lines))] += 1
        counters["transformed_line_count_bands"][_band(len(transformed_lines))] += 1
        counters["source_factual_line_count_bands"][_band(source_factual_count)] += 1
        counters["transformed_factual_line_count_bands"][_band(transformed_factual_count)] += 1
        counters["structural_line_count_bands"][_band(structural_count)] += 1
        counters["retained_section_count_bands"][_band(filtered.retained_section_count)] += 1
        counters["transformed_factual_retention_bands"][retention_band] += 1
        counters["review_outcome_by_retention_band"][f"{outcome}+{retention_band}"] += 1

        citation_roles = Counter(
            _line_citation_role(line, projections)
            for line in transformed_lines
            if CITATION_RE.search(line)
        )
        citation_combo = _citation_role_combination(citation_roles)
        counters["query_summary_cited_line_count_bands"][_band(citation_roles["summary_only"])] += 1
        counters["direct_source_cited_line_count_bands"][_band(citation_roles["direct_only"])] += 1
        counters["mixed_citation_line_count_bands"][_band(citation_roles["mixed"])] += 1
        counters["citation_role_combinations"][citation_combo] += 1
        counters["review_outcome_by_citation_role"][f"{outcome}+{citation_combo}"] += 1

        answer_contract = answer_contract_v6(
            candidate_answer=filtered.candidate_answer,
            projection_cache=cache,
        )
        answer_reason = _safe_code(answer_contract.reason)
        section_band = _band(answer_contract.section_count)
        counters["answer_contract_decisions"][_safe_code(answer_contract.decision)] += 1
        counters["answer_contract_reasons"][answer_reason] += 1
        counters["section_count_bands"][section_band] += 1
        counters["review_outcome_by_section_count_band"][f"{outcome}+{section_band}"] += 1
        counters["review_outcome_by_answer_contract_reason"][f"{outcome}+{answer_reason}"] += 1

        drop_fields = {
            "unsupported": filtered.dropped_unsupported_citation_line_count,
            "summary": filtered.dropped_summary_cited_line_count,
            "scope_mismatch": filtered.dropped_scope_mismatch_line_count,
            "mixed_citation": filtered.dropped_mixed_citation_line_count,
            "dropped_sections": filtered.dropped_section_count,
        }
        drop_reasons = [name for name, count in drop_fields.items() if count]
        drop_combo = _combo(drop_reasons, empty="none")
        counters["filter_drop_reason_combinations"][drop_combo] += 1
        counters["filter_unsupported_drop_count_bands"][_band(drop_fields["unsupported"])] += 1
        counters["filter_summary_drop_count_bands"][_band(drop_fields["summary"])] += 1
        counters["filter_scope_mismatch_drop_count_bands"][_band(drop_fields["scope_mismatch"])] += 1
        counters["filter_mixed_citation_drop_count_bands"][_band(drop_fields["mixed_citation"])] += 1
        counters["filter_dropped_section_count_bands"][_band(drop_fields["dropped_sections"])] += 1
        counters["filter_unknown_citation_drop_count_bands"][
            _band(filtered.dropped_unknown_citation_line_count)
        ] += 1
        counters["filter_internal_drop_count_bands"][
            _band(filtered.dropped_internal_line_count)
        ] += 1
        counters["filter_orphan_structure_drop_count_bands"][
            _band(filtered.dropped_orphan_structural_line_count)
        ] += 1
        if drop_reasons:
            for reason in drop_reasons:
                counters["review_outcome_by_filter_drop_reason"][
                    f"{outcome}+{reason}"
                ] += 1
        else:
            counters["review_outcome_by_filter_drop_reason"][f"{outcome}+none"] += 1

        counters["omission_notice_count_bands"][_band(filtered.omission_notice_count)] += 1
        counters["selected_candidate_score_bands"][_score_band(review.candidate_score)] += 1
        counters["selected_preferred_answer"][_safe_code(review.preferred_answer)] += 1

    artifact = SafetyFailureDiagnosticV6(
        experiment_id=selection.experiment_id,
        generated_at=generated_at or _utcnow(),
        source_holdout_hash=holdout.content_hash,
        source_selection_hash=selection.content_hash,
        source_review_hash=source_reviews.content_hash,
        transformed_review_hash=transformed_reviews.content_hash,
        reviewed_case_count=len(records),
        complete_review_count=complete_count,
        issue_case_count=issue_case_count,
        candidate_issue_count=candidate_issue_count,
        aggregate_counts={
            family: dict(sorted(values.items()))
            for family, values in sorted(counters.items())
        },
        privacy={
            "plaintext_persisted": False,
            "per_case_records_persisted": False,
            "identifying_hashes_persisted": False,
            "questions_persisted": False,
            "answers_persisted": False,
            "sources_persisted": False,
            "references_persisted": False,
            "rationales_persisted": False,
            "aggregate_metadata_only": True,
        },
    )
    return artifact.model_copy(update={"content_hash": content_hash(artifact)})


def analyze_policy_v6_safety_failure_from_paths(
    *,
    artifact_dir: Any,
    transformed_reviews_path: Any,
    output_path: Any,
    generated_at: datetime | None = None,
) -> SafetyFailureDiagnosticV6:
    """Load sealed inputs, build the aggregate diagnostic, and write it atomically."""

    root = Path(artifact_dir)
    artifact = analyze_policy_v6_safety_failure(
        holdout=read_model(root / "holdout.json", HoldoutCollection),
        selection=read_model(root / "selection.json", SelectionManifest),
        source_reviews=read_model(root / "reviews.json", AutomatedReviewBatch),
        transformed_reviews=read_model(
            Path(transformed_reviews_path),
            TransformedReviewArtifactV6,
        ),
        preregistration=read_model(root / "preregistration.json", Preregistration),
        generated_at=generated_at,
    )
    write_model(Path(output_path), artifact)
    return artifact


__all__ = [
    "SafetyFailureDiagnosticV6",
    "analyze_policy_v6_safety_failure",
    "analyze_policy_v6_safety_failure_from_paths",
]
