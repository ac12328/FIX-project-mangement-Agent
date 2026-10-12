"""Aggregate-only safety diagnostics for offline grounding-repair Policy-v5.

Sensitive holdout payloads and transformed review rows are decrypted only in memory.
The returned artifact contains cohort-level counters and artifact lineage; it never
contains per-case identifiers, questions, answers, sources, rationales, or hashes.
"""

from __future__ import annotations

import re
from collections import Counter
from datetime import datetime, timezone
from typing import Any, Iterable, Literal

from pydantic import Field, model_validator

from src.assistant.evals.artifacts import content_hash, read_model, write_model
from src.assistant.evals.grounding_repair_candidate_v5 import (
    filter_grounding_repair_candidate_v5,
)
from src.assistant.evals.grounding_repair_certificate_v5_projection import (
    ProjectionCacheV5,
)
from src.assistant.evals.grounding_repair_certificate_v5_rules import (
    ACTIVE_SCOPE_RE,
    CITATION_RE,
    LineContractCacheV5,
    POLICY_VERSION,
    UNIVERSAL_SCOPE_RE,
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
from src.assistant.evals.grounding_repair_v5_development import _case_payload, _oracle_safe
from src.assistant.evals.grounding_repair_v5_review import (
    TransformedReviewArtifactV5,
    decrypt_transformed_reviews_v5,
    transformed_candidate_hash_v5,
    transformed_case_payload_hash_v5,
)

_SAFE_CODE_RE = re.compile(r"^[A-Za-z0-9_.:+-]+$")
_BANDS = (0, 1, 3, 6, 11, 21, 41, 81)


class SafetyFailureDiagnosticV5(V4Model):
    """Plaintext-free aggregate diagnosis of the Policy-v5 safety-gate failure."""

    schema_version: Literal[1] = 1
    experiment_id: str = Field(min_length=3)
    generated_at: datetime
    policy_version: Literal[POLICY_VERSION] = POLICY_VERSION
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
    def validate_aggregate_only(self) -> "SafetyFailureDiagnosticV5":
        forbidden = {
            "question", "answer", "candidate_answer", "baseline_answer", "sources",
            "reference", "rationale", "records", "question_hash", "request_hash",
            "case_payload_hash", "run_id", "turn_id", "trace_id", "event_id",
        }
        for family, counts in self.aggregate_counts.items():
            if family.casefold() in forbidden:
                raise ValueError("diagnostic family exposes a forbidden payload field")
            if any(value < 0 for value in counts.values()):
                raise ValueError("diagnostic aggregate counts cannot be negative")
            if any(not _SAFE_CODE_RE.fullmatch(str(key)) for key in counts):
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


def _line_citation_role(
    line: str,
    projections: dict[str, Any],
) -> str:
    cited = [projections[source_id] for source_id in CITATION_RE.findall(line) if source_id in projections]
    if not cited:
        return "uncited"
    has_summary = any(item.is_query_summary for item in cited)
    has_direct = any(not item.is_query_summary for item in cited)
    if has_summary and has_direct:
        return "mixed"
    return "summary_only" if has_summary else "direct_only"


def _answer_scope_features(answer: str, query_scopes: set[str], citation_roles: Counter[str]) -> str:
    features: list[str] = []
    if ACTIVE_SCOPE_RE.search(answer):
        features.append("active_language")
    if UNIVERSAL_SCOPE_RE.search(answer):
        features.append("universal_language")
    if len(query_scopes) > 1:
        features.append("mixed_query_scopes")
    if citation_roles.get("summary_only"):
        features.append("summary_claims")
    if citation_roles.get("mixed"):
        features.append("mixed_citations")
    if citation_roles.get("direct_only"):
        features.append("direct_claims")
    return _combo(features, empty="no_scope_feature")


def _validate_lineage(
    *,
    holdout: HoldoutCollection,
    selection: SelectionManifest,
    source_reviews: AutomatedReviewBatch,
    transformed_reviews: TransformedReviewArtifactV5,
) -> None:
    if not all(verify_v4(item) for item in (holdout, selection, source_reviews, transformed_reviews)):
        raise ValueError("Policy-v5 diagnostic source artifact hash is invalid")
    if not (
        selection.holdout_hash == holdout.content_hash
        and source_reviews.selection_hash == selection.content_hash
        and transformed_reviews.source_holdout_hash == holdout.content_hash
        and transformed_reviews.source_selection_hash == selection.content_hash
        and transformed_reviews.source_review_hash == source_reviews.content_hash
        and transformed_reviews.status == "complete"
    ):
        raise ValueError("Policy-v5 diagnostic source lineage is invalid")


def analyze_policy_v5_safety_failure(
    *,
    holdout: HoldoutCollection,
    selection: SelectionManifest,
    source_reviews: AutomatedReviewBatch,
    transformed_reviews: TransformedReviewArtifactV5,
    preregistration: Preregistration,
    generated_at: datetime | None = None,
) -> SafetyFailureDiagnosticV5:
    """Build aggregate failure clusters while keeping all case payloads in memory."""

    _validate_lineage(
        holdout=holdout,
        selection=selection,
        source_reviews=source_reviews,
        transformed_reviews=transformed_reviews,
    )
    records = decrypt_transformed_reviews_v5(transformed_reviews)
    if len(records) != transformed_reviews.review_target_count:
        raise ValueError("Policy-v5 diagnostic transformed review coverage is incomplete")

    cases = {item.question_hash: item for item in holdout.cases}
    source_review_by_hash = {item.question_hash: item for item in source_reviews.reviews}
    counters: dict[str, Counter[str]] = {
        "issue_code_combinations": Counter(),
        "individual_issue_codes": Counter(),
        "tool_combinations": Counter(),
        "query_scope_combinations": Counter(),
        "source_v4_safety": Counter(),
        "source_v4_to_transformed_safety": Counter(),
        "original_line_count_bands": Counter(),
        "transformed_line_count_bands": Counter(),
        "transformed_retention_bands": Counter(),
        "factual_line_count_bands": Counter(),
        "structural_line_count_bands": Counter(),
        "query_summary_cited_line_count_bands": Counter(),
        "direct_source_cited_line_count_bands": Counter(),
        "mixed_citation_line_count_bands": Counter(),
        "answer_scope_feature_combinations": Counter(),
        "candidate_score_bands": Counter(),
        "preferred_answer": Counter(),
    }
    complete_count = issue_case_count = candidate_issue_count = 0
    contract_cache = LineContractCacheV5()

    for record in records:
        case = cases.get(record.source_question_hash)
        source_review = source_review_by_hash.get(record.source_question_hash)
        if case is None or source_review is None or case.case_payload_hash is None:
            raise ValueError("Policy-v5 diagnostic case lineage is incomplete")
        payload = _case_payload(case)
        if payload is None:
            raise ValueError("Policy-v5 diagnostic encrypted payload is invalid")
        sources = [item for item in payload.get("sources") or [] if isinstance(item, dict)]
        tool_results = [item for item in payload.get("tool_results") or [] if isinstance(item, dict)]
        cache = ProjectionCacheV5.build(sources, tool_results)
        filtered = filter_grounding_repair_candidate_v5(
            candidate_answer=str(payload.get("candidate_answer") or ""),
            question=str(payload.get("question") or ""),
            sources=sources,
            tool_results=tool_results,
            projection_cache=cache,
            contract_cache=contract_cache,
        )
        candidate_hash = transformed_candidate_hash_v5(filtered.candidate_answer)
        payload_hash = transformed_case_payload_hash_v5(
            source_case_payload_hash=case.case_payload_hash,
            transformed_candidate_hash=candidate_hash,
        )
        if (
            record.source_case_payload_hash != case.case_payload_hash
            or record.transformed_candidate_hash != candidate_hash
            or record.transformed_case_payload_hash != payload_hash
            or record.review.case_payload_hash != payload_hash
        ):
            raise ValueError("Policy-v5 diagnostic transformed record does not match implementation")

        review = record.review
        if review.complete:
            complete_count += 1
        issue_codes = [_safe_code(code) for code in review.candidate_issue_codes]
        if issue_codes:
            issue_case_count += 1
            candidate_issue_count += len(issue_codes)
        counters["issue_code_combinations"][_combo(issue_codes)] += 1
        counters["individual_issue_codes"].update(issue_codes)
        tools = [_safe_code(item.get("tool")) for item in tool_results if item.get("tool") != "think"]
        counters["tool_combinations"][_combo(tools)] += 1

        cited_ids = set(CITATION_RE.findall(filtered.candidate_answer))
        projections = cache.materialize(cited_ids)
        scopes = {item.query_scope for item in projections.values() if item.query_scope}
        counters["query_scope_combinations"][_combo(scopes)] += 1

        source_safe = _oracle_safe(source_review, preregistration)
        transformed_safe = bool(review.complete and not review.candidate_issue_codes)
        counters["source_v4_safety"]["safe" if source_safe else "unsafe"] += 1
        transition = f"source_{'safe' if source_safe else 'unsafe'}_to_{'safe' if transformed_safe else 'unsafe'}"
        counters["source_v4_to_transformed_safety"][transition] += 1

        original_lines = str(payload.get("candidate_answer") or "").splitlines()
        transformed_lines = filtered.candidate_answer.splitlines()
        counters["original_line_count_bands"][_band(len(original_lines))] += 1
        counters["transformed_line_count_bands"][_band(len(transformed_lines))] += 1
        counters["transformed_retention_bands"][_ratio_band(len(transformed_lines), len(original_lines))] += 1

        line_kinds = Counter(classify_line_v5(line.strip()) for line in transformed_lines)
        counters["factual_line_count_bands"][_band(line_kinds["factual"])] += 1
        counters["structural_line_count_bands"][_band(line_kinds["structural"])] += 1
        citation_roles = Counter(
            _line_citation_role(line, projections)
            for line in transformed_lines
            if CITATION_RE.search(line)
        )
        counters["query_summary_cited_line_count_bands"][_band(citation_roles["summary_only"])] += 1
        counters["direct_source_cited_line_count_bands"][_band(citation_roles["direct_only"])] += 1
        counters["mixed_citation_line_count_bands"][_band(citation_roles["mixed"])] += 1
        counters["answer_scope_feature_combinations"][
            _answer_scope_features(filtered.candidate_answer, scopes, citation_roles)
        ] += 1
        counters["candidate_score_bands"][_score_band(review.candidate_score)] += 1
        counters["preferred_answer"][_safe_code(review.preferred_answer)] += 1

    artifact = SafetyFailureDiagnosticV5(
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
            family: dict(sorted(values.items())) for family, values in sorted(counters.items())
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


def analyze_policy_v5_safety_failure_from_paths(
    *,
    artifact_dir: Any,
    transformed_reviews_path: Any,
    output_path: Any,
    generated_at: datetime | None = None,
) -> SafetyFailureDiagnosticV5:
    """Load sealed inputs, build the aggregate diagnostic, and write it atomically."""

    root = __import__("pathlib").Path(artifact_dir)
    artifact = analyze_policy_v5_safety_failure(
        holdout=read_model(root / "holdout.json", HoldoutCollection),
        selection=read_model(root / "selection.json", SelectionManifest),
        source_reviews=read_model(root / "reviews.json", AutomatedReviewBatch),
        transformed_reviews=read_model(
            __import__("pathlib").Path(transformed_reviews_path),
            TransformedReviewArtifactV5,
        ),
        preregistration=read_model(root / "preregistration.json", Preregistration),
        generated_at=generated_at,
    )
    write_model(__import__("pathlib").Path(output_path), artifact)
    return artifact


__all__ = ["SafetyFailureDiagnosticV5", "analyze_policy_v5_safety_failure"]
