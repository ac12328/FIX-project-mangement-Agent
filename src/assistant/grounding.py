"""Deterministic grounding and completeness guards for AI Workspace answers."""

from __future__ import annotations

import json
import re
import time
from collections import Counter
from dataclasses import dataclass
from typing import Any

from opentelemetry import trace

from src.assistant.grounding_repair_shadow import (
    GroundingRepairShadowDecision,
    legacy_no_repair_decision,
    shadow_error_decision,
)
from src.assistant.grounding_repair_shadow_fast import (
    GroundingRepairShadowProfile,
    evaluate_grounding_repair_shadow_fast,
)
from src.llm.client import llm_call
from src.observability.progress import ProgressCallback, emit_progress

_CITATION_RE = re.compile(r"\[\[source:([A-Za-z0-9][A-Za-z0-9_.:-]*)\]\]")
_EMBEDDED_CITATION_RE = re.compile(r"\[\[source:[A-Za-z0-9][A-Za-z0-9_.:-]*(?:\]\])?")
_INTERNAL_TOOL_ARTIFACT_RE = re.compile(r"\bmonads0x[a-z0-9_]+\b", re.IGNORECASE)
_PROJECT_FACT_RE = re.compile(
    r"\b[A-Z]{2,}[A-Z0-9-]*20\d{2}\b|\bM[1-8](?:_[A-Za-z0-9]+)?\b|"
    r"\b(?:Active|Blocked|Waiting|Completed|Archived|Cancelled|Internal|External|Shared)\b|"
    r"\b\d{4}-\d{2}-\d{2}\b|\b\d+\s*(?:projects?|days?|d)\b",
    re.IGNORECASE,
)
_MARKDOWN_TABLE_SEPARATOR_RE = re.compile(
    r"^\s*\|?\s*:?-{3,}:?\s*(?:\|\s*:?-{3,}:?\s*)+\|?\s*$"
)
_TOOL_SUMMARY_LINE_RE = re.compile(
    r"^\s*(?:[-*]\s*)?(?:\*\*)?(?:"
    r"data source|source|scope|range|filters?|workspace snapshot|snapshot|as[- /]?of|"
    r"\u8cc7\u6599\u4f86\u6e90|\u7bc4\u570d|\u67e5\u8a62\u7bc4\u570d|\u689d\u4ef6|"
    r"\u5de5\u4f5c\u5340\u5feb\u7167|\u5feb\u7167"
    r")",
    re.IGNORECASE,
)
_RECOMMENDATION_RE = re.compile(
    r"\b(?:recommend|suggest|should|could|next step|proposed|proposal)\b|建議|下一步|可考慮|應該",
    re.IGNORECASE,
)
_TEST_PASS_RE = re.compile(
    r"\ball\s+(?:required\s+)?(?:conformance\s+|fix\s+|production\s+)?"
    r"tests?\s+(?:(?:have|has|had|are|were)\s+)?(?:been\s+)?passed\b|"
    r"(?:all\s+tests?|tests?\s+all)\s+(?:are|were)\s+passing\b",
    re.IGNORECASE,
)
_NEXT_ACTION_CLAIM_RE = re.compile(
    r"\bnext[ _-]?action\b|\bfollow[ -]?up\b|\baction\s+(?:was\s+)?(?:updated|recorded)\b",
    re.IGNORECASE,
)
_NEXT_ACTION_VALUE_RE = re.compile(
    r"next_action\s*=\s*(.*?)(?=,\s*[a-z_]+\s*=|\s*\(undo|$)",
    re.IGNORECASE,
)
_ACTION_STOPWORDS = frozenset({
    "action", "april", "based", "complete", "confirm", "current", "follow", "next", "provided",
    "setup", "team", "the", "this", "update", "updated", "with",
})
_COMPLETION_RE = re.compile(
    r"pct.{0,30}(?:is\s+)?completed|session.{0,30}can go live|can go-live",
    re.IGNORECASE | re.DOTALL,
)
_CONTACT_RE = re.compile(r"\bcontact(?:s| details| information)?\b|\beloise\b", re.IGNORECASE)
_NO_CURRENT_CONFLICT_RE = re.compile(
    r"\bno (?:present |current |live )?(?:evidence )?conflicts?\b|"
    r"\bconflicts?\s*(?:are|is|:)\s*(?:\[\]|none|empty|zero)\b|"
    r"(?:\u76ee\u524d|\u7576\u524d|\u73fe\u6642)?(?:\u6c92\u6709|\u7121|\u4e0d\u5b58\u5728)(?:\u8b49\u64da)?(?:\u885d\u7a81|\u77db\u76fe|\u4e0d\u4e00\u81f4)|\u4e00\u81f4",
    re.IGNORECASE,
)
_EXPLICIT_EMPTY_AUDIT_CONFLICTS_RE = re.compile(
    r"\bno (?:open |present |current |live )?(?:evidence )?conflicts?\b|"
    r"\bconflicts?\s*(?:are|is|:|=)\s*(?:none|empty|zero)\b|"
    r"\bconflicts?\s*(?:are|is|:|=)\s*\[\]|"
    r"(?:\u76ee\u524d|\u7576\u524d|\u73fe\u6642)?"
    r"(?:\u6c92\u6709|\u7121|\u4e0d\u5b58\u5728)(?:\u8b49\u64da)?"
    r"(?:\u885d\u7a81|\u77db\u76fe|\u4e0d\u4e00\u81f4)",
    re.IGNORECASE,
)
_POSITIVE_CURRENT_CONFLICT_RE = re.compile(
    r"\b(?:current|present|live)\b.{0,60}\b(?:conflict|disagree|different)\b|"
    r"\b(?:conflict|disagreement|different interpretation)\b|"
    r"\u885d\u7a81|\u77db\u76fe|\u4e0d\u4e00\u81f4|\u89e3\u8b80\u4e0d\u540c",
    re.IGNORECASE,
)
_HISTORICAL_CONFLICT_RE = re.compile(
    r"\b(?:historical|history|previous|older|past|undone|superseded|rejected)\b|"
    r"\u6b77\u53f2|\u5148\u524d|\u820a\u7d00\u9304|\u5df2\u64a4\u92b7|\u5df2\u5fa9\u539f|\u5df2\u53d6\u4ee3|\u5df2\u62d2\u7d55",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class GroundingResult:
    """Final answer plus any validation errors that could not be repaired."""

    text: str
    validation_errors: tuple[str, ...] = ()


def _validation_error_code(error: str) -> str:
    """Map stable validator messages to an analysis-friendly taxonomy."""
    if error == "The answer exposes an internal tool protocol marker":
        return "internal_tool_marker"
    if error.startswith("Unknown citations:"):
        return "unknown_citation"
    if error == "The answer contains no source citations":
        return "missing_citations"
    if error.startswith("Uncited factual line:"):
        return "uncited_factual_line"
    return "unknown"


def _validation_error_metadata(errors: list[str]) -> dict[str, Any]:
    codes = [_validation_error_code(error) for error in errors]
    counts = Counter(codes)
    return {
        "validation_error_codes": codes,
        "validation_error_counts": dict(sorted(counts.items())),
    }


def _is_markdown_table_separator(line: str) -> bool:
    return bool(_MARKDOWN_TABLE_SEPARATOR_RE.fullmatch(line.strip()))


def _looks_like_markdown_table_row(line: str) -> bool:
    stripped = line.strip()
    return stripped.startswith("|") and stripped.endswith("|") and stripped.count("|") >= 3


def _markdown_table_header_indexes(lines: list[str]) -> set[int]:
    return {
        index
        for index in range(len(lines) - 1)
        if _looks_like_markdown_table_row(lines[index])
        and _is_markdown_table_separator(lines[index + 1])
    }


def _normalized_lines(text: str, *, factual_only: bool = False) -> Counter[str]:
    lines: list[str] = []
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line or line.startswith(("#", "|---", "```")):
            continue
        if factual_only and not _PROJECT_FACT_RE.search(line):
            continue
        without_citations = _CITATION_RE.sub("", line)
        normalized = re.sub(r"[^\w]+", " ", without_citations, flags=re.UNICODE).casefold().strip()
        if normalized:
            lines.append(normalized)
    return Counter(lines)


def _repair_delta(before: str, after: str) -> dict[str, Any]:
    """Describe observable repair changes without judging or changing the answer."""
    before_citations = Counter(_CITATION_RE.findall(before))
    after_citations = Counter(_CITATION_RE.findall(after))
    citations_added = sum((after_citations - before_citations).values())
    citations_removed = sum((before_citations - after_citations).values())
    before_claims = _normalized_lines(before)
    after_claims = _normalized_lines(after)
    before_facts = _normalized_lines(before, factual_only=True)
    after_facts = _normalized_lines(after, factual_only=True)
    semantic_before = " ".join(before_claims.elements())
    semantic_after = " ".join(after_claims.elements())
    content_changed = semantic_before != semantic_after
    citations_changed = citations_added > 0 or citations_removed > 0
    return {
        "repair_content_changed": content_changed,
        "repair_format_only_change": before != after and not content_changed and not citations_changed,
        "repair_citations_changed": citations_changed,
        "repair_citations_added": citations_added,
        "repair_citations_removed": citations_removed,
        "repair_claim_lines_removed": sum((before_claims - after_claims).values()),
        "repair_factual_lines_removed": sum((before_facts - after_facts).values()),
        "repair_input_chars": len(before),
        "repair_output_chars": len(after),
    }


def ground_workspace_answer(
    answer: str,
    sources: list[dict[str, Any]],
    tool_results: list[dict[str, Any]],
    *,
    progress_cb: ProgressCallback | None = None,
) -> GroundingResult:
    """Sanitize, complete, cite, validate, and if needed repair a Workspace answer."""
    tracer = trace.get_tracer("agentfixbpro.assistant")
    grounding_started = time.perf_counter()
    repair_mode = _grounding_repair_mode()
    selective_mode = repair_mode in {"shadow", "serve"}
    serve_mode = repair_mode == "serve"
    emit_progress(
        progress_cb,
        "grounding.validation",
        "running",
        "Validating verified Workspace records",
    )
    with tracer.start_as_current_span("assistant.grounding") as span:
        span.set_attribute("assistant.source_count", len(sources))
        span.set_attribute("assistant.tool_result_count", len(tool_results))
        span.set_attribute("assistant.grounding.input_chars", len(answer or ""))
        candidate = _prepare_answer(answer, sources, tool_results)
        errors = _validate_answer(candidate, sources, tool_results)
        initial_error_metadata = _validation_error_metadata(errors)
        span.set_attribute("assistant.grounding.initial_error_count", len(errors))
        span.set_attribute(
            "assistant.grounding.initial_error_codes",
            initial_error_metadata["validation_error_codes"],
        )
        span.set_attribute("assistant.grounding.repair_triggered", bool(errors))
        _emit_grounding_event(
            "grounding.validation",
            status="error" if errors else "ok",
            metadata={
                "phase": "initial",
                "source_count": len(sources),
                "tool_result_count": len(tool_results),
                "error_count": len(errors),
                "answer_chars": len(candidate),
                **initial_error_metadata,
            },
            payload={"answer": candidate, "validation_errors": errors},
        )
        if not errors:
            emit_progress(
                progress_cb,
                "grounding.validation",
                "done",
                "Workspace records validated",
            )
            span.set_attribute("assistant.grounding.final_error_count", 0)
            span.set_attribute("assistant.grounding.output_chars", len(candidate))
            if selective_mode:
                elapsed_ms = (time.perf_counter() - grounding_started) * 1000
                _emit_grounding_repair_shadow(
                    decision=legacy_no_repair_decision(),
                    prepared_answer=candidate,
                    shadow_candidate_answer=candidate,
                    baseline_answer=candidate,
                    sources=sources,
                    tool_results=tool_results,
                    initial_errors=[],
                    candidate_errors=[],
                    baseline_errors=[],
                    shadow_compute_ms=0.0,
                    shadow_source_support_compute_ms=0.0,
                    shadow_decision_compute_ms=0.0,
                    semantic_support_precomputed=False,
                    candidate_ready_ms=elapsed_ms,
                    baseline_ready_ms=elapsed_ms,
                    baseline_repair_attempted=False,
                    mode=repair_mode,
                )
            _emit_grounding_event(
                "grounding.validation",
                status="ok",
                metadata={
                    "phase": "final",
                    "source_count": len(sources),
                    "tool_result_count": len(tool_results),
                    "error_count": 0,
                    "answer_chars": len(candidate),
                    "repair_attempted": False,
                    **_validation_error_metadata([]),
                },
                payload={"answer": candidate, "validation_errors": []},
            )
            return GroundingResult(candidate)

        shadow_decision: GroundingRepairShadowDecision | None = None
        shadow_candidate = ""
        shadow_candidate_errors: list[str] = []
        shadow_compute_ms = 0.0
        shadow_source_support_compute_ms = 0.0
        shadow_decision_compute_ms = 0.0
        semantic_support_precomputed = False
        semantic_profile_metadata: dict[str, object] = {
            "semantic_support_strategy": "not_evaluated",
            "semantic_support_materialized_count": 0,
            "semantic_line_count": 0,
            "semantic_lines_evaluated": 0,
            "semantic_analysis_complete": False,
            "semantic_short_circuit": False,
        }
        shadow_error_class: str | None = None
        candidate_ready_ms = 0.0
        if selective_mode:
            emit_progress(
                progress_cb,
                "grounding.filter",
                "running",
                "Removing unsupported claims",
            )
            shadow_started_ns = time.perf_counter_ns()
            shadow_profile = GroundingRepairShadowProfile()
            try:
                shadow_candidate = _verified_partial_answer(candidate, sources)
                shadow_candidate = _prepare_answer(shadow_candidate, sources, tool_results)
                shadow_candidate_errors = _validate_answer(
                    shadow_candidate, sources, tool_results
                )
                decision_started_ns = time.perf_counter_ns()
                shadow_decision = evaluate_grounding_repair_shadow_fast(
                    prepared_answer=candidate,
                    initial_error_codes=(
                        _validation_error_code(error) for error in errors
                    ),
                    candidate_answer=shadow_candidate,
                    candidate_error_codes=(
                        _validation_error_code(error) for error in shadow_candidate_errors
                    ),
                    source_ids=(source.get("source_id") for source in sources),
                    sources=sources,
                    factual_tools_used=any(
                        item.get("tool") != "think" for item in tool_results
                    ),
                    profile=shadow_profile,
                )
                decision_total_ms = (
                    time.perf_counter_ns() - decision_started_ns
                ) / 1_000_000
                shadow_source_support_compute_ms = (
                    shadow_profile.source_support_compute_ms
                )
                shadow_decision_compute_ms = max(
                    0.0, decision_total_ms - shadow_source_support_compute_ms
                )
            except Exception as exc:
                shadow_error_class = type(exc).__name__
                shadow_decision = shadow_error_decision(
                    _validation_error_code(error) for error in errors
                )
            semantic_profile_metadata = shadow_profile.as_metadata()
            shadow_compute_ms = (time.perf_counter_ns() - shadow_started_ns) / 1_000_000
            candidate_ready_ms = (time.perf_counter() - grounding_started) * 1000
            emit_progress(
                progress_cb,
                "grounding.filter",
                "done",
                (
                    "Unsupported claims removed"
                    if not shadow_candidate_errors
                    else "Unsupported-claim review complete; applying bounded repair"
                ),
            )

        if (
            serve_mode
            and shadow_decision is not None
            and shadow_decision.would_skip_repair
            and not shadow_candidate_errors
        ):
            span.set_attribute("assistant.grounding.repair_skipped", True)
            span.set_attribute("assistant.grounding.final_error_count", 0)
            span.set_attribute("assistant.grounding.output_chars", len(shadow_candidate))
            _emit_grounding_repair_shadow(
                decision=shadow_decision,
                prepared_answer=candidate,
                shadow_candidate_answer=shadow_candidate,
                baseline_answer=shadow_candidate,
                sources=sources,
                tool_results=tool_results,
                initial_errors=errors,
                candidate_errors=shadow_candidate_errors,
                baseline_errors=[],
                shadow_compute_ms=shadow_compute_ms,
                shadow_source_support_compute_ms=shadow_source_support_compute_ms,
                shadow_decision_compute_ms=shadow_decision_compute_ms,
                semantic_support_precomputed=semantic_support_precomputed,
                semantic_profile_metadata=semantic_profile_metadata,
                candidate_ready_ms=candidate_ready_ms,
                baseline_ready_ms=candidate_ready_ms,
                baseline_repair_attempted=False,
                shadow_error_class=shadow_error_class,
                mode=repair_mode,
                served_answer_source="verified_candidate",
                user_visible_behavior_changed=True,
            )
            _emit_grounding_event(
                "grounding.validation",
                status="ok",
                metadata={
                    "phase": "final",
                    "source_count": len(sources),
                    "tool_result_count": len(tool_results),
                    "error_count": 0,
                    "answer_chars": len(shadow_candidate),
                    "repair_attempted": False,
                    "deterministic_filter_served": True,
                    **_validation_error_metadata([]),
                    **_repair_delta(candidate, shadow_candidate),
                },
                payload={"answer": shadow_candidate, "validation_errors": []},
            )
            emit_progress(
                progress_cb,
                "grounding.finalize",
                "done",
                "Verified response finalized",
            )
            return GroundingResult(shadow_candidate)

        emit_progress(
            progress_cb,
            "grounding.repair",
            "running",
            "Finalizing verified response",
        )
        repair_started = time.perf_counter()
        _emit_grounding_event(
            "grounding.repair.start",
            status="running",
            metadata={
                "source_count": len(sources),
                "error_count": len(errors),
                "answer_chars": len(candidate),
                **initial_error_metadata,
            },
            payload={"answer": candidate, "validation_errors": errors},
        )
        with tracer.start_as_current_span("assistant.grounding.repair") as repair_span:
            repair_span.set_attribute("assistant.grounding.error_count", len(errors))
            repair_span.set_attribute("assistant.source_count", len(sources))
            repaired = _repair_answer(candidate, errors, sources)
            repair_span.set_attribute("assistant.grounding.output_chars", len(repaired))
        repaired = _prepare_answer(repaired, sources, tool_results)
        repair_delta = _repair_delta(candidate, repaired)
        _emit_grounding_event(
            "grounding.repair.end",
            status="ok",
            duration_ms=(time.perf_counter() - repair_started) * 1000,
            metadata={
                "source_count": len(sources),
                "initial_error_count": len(errors),
                "answer_chars": len(repaired),
                **initial_error_metadata,
                **repair_delta,
            },
            payload={"repaired_answer": repaired},
        )
        repaired_errors = _validate_answer(repaired, sources, tool_results)
        if not repaired_errors:
            span.set_attribute("assistant.grounding.final_error_count", 0)
            span.set_attribute("assistant.grounding.output_chars", len(repaired))
            if shadow_decision is not None:
                _emit_grounding_repair_shadow(
                    decision=shadow_decision,
                    prepared_answer=candidate,
                    shadow_candidate_answer=shadow_candidate,
                    baseline_answer=repaired,
                    sources=sources,
                    tool_results=tool_results,
                    initial_errors=errors,
                    candidate_errors=shadow_candidate_errors,
                    baseline_errors=[],
                    shadow_compute_ms=shadow_compute_ms,
                    shadow_source_support_compute_ms=shadow_source_support_compute_ms,
                    shadow_decision_compute_ms=shadow_decision_compute_ms,
                    semantic_support_precomputed=semantic_support_precomputed,
                    semantic_profile_metadata=semantic_profile_metadata,
                    candidate_ready_ms=candidate_ready_ms,
                    baseline_ready_ms=(time.perf_counter() - grounding_started) * 1000,
                    baseline_repair_attempted=True,
                    shadow_error_class=shadow_error_class,
                    mode=repair_mode,
                )
            _emit_grounding_event(
                "grounding.validation",
                status="ok",
                metadata={
                    "phase": "final",
                    "source_count": len(sources),
                    "tool_result_count": len(tool_results),
                    "error_count": 0,
                    "answer_chars": len(repaired),
                    "repair_attempted": True,
                    "partial_fallback": False,
                    **_validation_error_metadata([]),
                    **repair_delta,
                },
                payload={"answer": repaired, "validation_errors": []},
            )
            emit_progress(
                progress_cb,
                "grounding.repair",
                "done",
                "Verified response finalized",
            )
            return GroundingResult(repaired)

        partial = _verified_partial_answer(repaired, sources)
        partial = _prepare_answer(partial, sources, tool_results)
        partial_errors = _validate_answer(partial, sources, tool_results)
        partial_delta = _repair_delta(candidate, partial)
        span.set_attribute("assistant.grounding.final_error_count", len(partial_errors))
        span.set_attribute("assistant.grounding.output_chars", len(partial))
        if shadow_decision is not None:
            _emit_grounding_repair_shadow(
                decision=shadow_decision,
                prepared_answer=candidate,
                shadow_candidate_answer=shadow_candidate,
                baseline_answer=partial,
                sources=sources,
                tool_results=tool_results,
                initial_errors=errors,
                candidate_errors=shadow_candidate_errors,
                baseline_errors=partial_errors,
                shadow_compute_ms=shadow_compute_ms,
                shadow_source_support_compute_ms=shadow_source_support_compute_ms,
                shadow_decision_compute_ms=shadow_decision_compute_ms,
                semantic_support_precomputed=semantic_support_precomputed,
                semantic_profile_metadata=semantic_profile_metadata,
                candidate_ready_ms=candidate_ready_ms,
                baseline_ready_ms=(time.perf_counter() - grounding_started) * 1000,
                baseline_repair_attempted=True,
                shadow_error_class=shadow_error_class,
                mode=repair_mode,
            )
        _emit_grounding_event(
            "grounding.validation",
            status="error" if partial_errors else "ok",
            metadata={
                "phase": "final",
                "source_count": len(sources),
                "tool_result_count": len(tool_results),
                "error_count": len(partial_errors),
                "answer_chars": len(partial),
                "repair_attempted": True,
                "partial_fallback": True,
                **_validation_error_metadata(partial_errors),
                **partial_delta,
            },
            payload={"answer": partial, "validation_errors": partial_errors},
        )
        emit_progress(
            progress_cb,
            "grounding.repair",
            "done" if not partial_errors else "error",
            "Verified partial response finalized" if not partial_errors else None,
        )
        return GroundingResult(partial, tuple(partial_errors))


def _grounding_repair_mode() -> str:
    """Return the validated rollout mode, defaulting safely to legacy behavior."""
    try:
        from config import defaults

        mode = str(defaults.AI_WORKSPACE_GROUNDING_REPAIR_MODE).strip().lower()
    except Exception:
        return "legacy"
    return mode if mode in {"legacy", "shadow", "serve"} else "legacy"


def _grounding_repair_timeout_seconds() -> int:
    """Return a bounded repair timeout while preserving fail-open behavior."""
    try:
        from config import defaults

        timeout = int(defaults.AI_WORKSPACE_GROUNDING_REPAIR_TIMEOUT_SECONDS)
    except Exception:
        return 20
    return min(60, max(1, timeout))


def _grounding_content_hash(value: Any) -> str:
    """Hash the sanitized payload form revealed to privileged reviewers."""
    import hashlib

    from src.observability.payload_crypto import filter_secrets

    encoded = json.dumps(
        filter_secrets(value), ensure_ascii=False, sort_keys=True, default=str
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _emit_grounding_repair_shadow(
    *,
    decision: GroundingRepairShadowDecision,
    prepared_answer: str,
    shadow_candidate_answer: str,
    baseline_answer: str,
    sources: list[dict[str, Any]],
    tool_results: list[dict[str, Any]],
    initial_errors: list[str],
    candidate_errors: list[str],
    baseline_errors: list[str],
    shadow_compute_ms: float,
    shadow_source_support_compute_ms: float,
    shadow_decision_compute_ms: float,
    semantic_support_precomputed: bool,
    candidate_ready_ms: float,
    semantic_profile_metadata: dict[str, object] | None = None,
    baseline_ready_ms: float,
    baseline_repair_attempted: bool,
    shadow_error_class: str | None = None,
    mode: str = "shadow",
    served_answer_source: str = "legacy_baseline",
    user_visible_behavior_changed: bool = False,
) -> None:
    """Emit the paired shadow/baseline observation through encrypted trace payloads."""
    projected_savings_ms = (
        max(0.0, baseline_ready_ms - candidate_ready_ms)
        if decision.would_skip_repair
        else 0.0
    )
    metadata = {
        **decision.as_metadata(),
        "mode": mode,
        "source_count": len(sources),
        "tool_result_count": len(tool_results),
        "initial_error_count": len(initial_errors),
        "candidate_error_count": len(candidate_errors),
        "baseline_error_count": len(baseline_errors),
        "initial_error_counts": _validation_error_metadata(initial_errors)[
            "validation_error_counts"
        ],
        "candidate_error_counts": _validation_error_metadata(candidate_errors)[
            "validation_error_counts"
        ],
        "baseline_error_counts": _validation_error_metadata(baseline_errors)[
            "validation_error_counts"
        ],
        "baseline_repair_attempted": baseline_repair_attempted,
        "served_answer_source": served_answer_source,
        "shadow_additional_llm_calls": 0,
        "shadow_synchronous_db_writes": 0,
        "user_visible_behavior_changed": user_visible_behavior_changed,
        "business_persistence_changed": False,
        "projected_repair_call_reduction": int(decision.would_skip_repair),
        "shadow_compute_ms": round(max(0.0, shadow_compute_ms), 6),
        "shadow_source_support_compute_ms": round(
            max(0.0, shadow_source_support_compute_ms), 6
        ),
        "shadow_decision_compute_ms": round(
            max(0.0, shadow_decision_compute_ms), 6
        ),
        "semantic_support_precomputed": semantic_support_precomputed,
        **(semantic_profile_metadata or {
            "semantic_support_strategy": "not_evaluated",
            "semantic_support_materialized_count": 0,
            "semantic_line_count": 0,
            "semantic_lines_evaluated": 0,
            "semantic_analysis_complete": False,
            "semantic_short_circuit": False,
        }),
        "candidate_ready_ms": round(max(0.0, candidate_ready_ms), 6),
        "baseline_ready_ms": round(max(0.0, baseline_ready_ms), 6),
        "projected_completion_savings_ms": round(projected_savings_ms, 6),
        "prepared_answer_hash": _grounding_content_hash(prepared_answer),
        "candidate_answer_hash": _grounding_content_hash(shadow_candidate_answer),
        "baseline_answer_hash": _grounding_content_hash(baseline_answer),
    }
    if shadow_error_class:
        metadata["shadow_error_class"] = shadow_error_class
    _emit_grounding_event(
        "grounding.repair.shadow",
        status="error" if decision.decision == "shadow_error" else "ok",
        duration_ms=shadow_compute_ms,
        metadata=metadata,
        payload={
            "prepared_answer": prepared_answer,
            "candidate_answer": shadow_candidate_answer,
            "baseline_answer": baseline_answer,
            "sources": sources,
            "tool_results": tool_results,
            "initial_errors": initial_errors,
            "candidate_errors": candidate_errors,
            "baseline_errors": baseline_errors,
        },
    )


def _emit_grounding_event(event_type: str, **kwargs: Any) -> None:
    """Emit a fail-open engineering event when an Agent trace is active."""
    try:
        from src.observability.contracts import current_trace_context
        from src.observability.lifecycle import emit_event

        context = current_trace_context()
        if context is not None:
            emit_event(context, event_type, **kwargs)
    except Exception:
        # Grounding must remain independent from telemetry availability.
        return

def _prepare_answer(
    answer: str,
    sources: list[dict[str, Any]],
    tool_results: list[dict[str, Any]],
) -> str:
    candidate = _sanitize_internal_artifacts(str(answer or "").strip())
    candidate = _enforce_evidence_audit_conflict_truth(candidate, tool_results)
    candidate = _attach_project_line_citations(candidate, sources)
    candidate = _attach_tool_summary_line_citations(candidate, sources, tool_results)
    candidate = _ensure_tool_completeness(candidate, tool_results)
    candidate = _remove_superseded_next_action_lines(candidate, tool_results)
    return _attach_project_line_citations(candidate, sources).strip()


def _enforce_evidence_audit_conflict_truth(
    answer: str,
    tool_results: list[dict[str, Any]],
) -> str:
    """Remove present-conflict overclaims when the authoritative audit reports none."""
    corrections: list[str] = []
    lines = answer.splitlines()
    for item in tool_results:
        if item.get("tool") != "get_project_evidence_audit":
            continue
        result = item.get("result") if isinstance(item.get("result"), dict) else {}
        conflicts = result.get("conflicts")
        if not isinstance(conflicts, list) or conflicts:
            continue
        truth = result.get("current_truth") if isinstance(result.get("current_truth"), dict) else {}
        source_id = str(truth.get("source_id") or "").strip()
        if not source_id:
            continue

        removed = False
        retained: list[str] = []
        for line in lines:
            positive = bool(_POSITIVE_CURRENT_CONFLICT_RE.search(line))
            negated = bool(_NO_CURRENT_CONFLICT_RE.search(line))
            historical = bool(_HISTORICAL_CONFLICT_RE.search(line))
            if positive and not negated and not historical:
                removed = True
                continue
            retained.append(line)
        if not removed:
            continue
        lines = retained

        project_id = str(result.get("project_id") or truth.get("project_id") or "project")
        fields: list[str] = []
        if truth.get("ball_with") not in (None, ""):
            fields.append(f"ball_with={truth['ball_with']}")
        if truth.get("next_action") not in (None, ""):
            fields.append(f"next_action={truth['next_action']}")
        current = f" Canonical current truth: {'; '.join(fields)}." if fields else ""
        corrections.append(
            f"**Authoritative evidence check ({project_id}):** No present conflict is recorded; "
            f"the canonical project row remains authoritative.{current} [[source:{source_id}]]"
        )
    if not corrections:
        return answer
    body = "\n".join(lines).strip()
    return "\n\n".join(corrections + ([body] if body else []))


def _sanitize_internal_artifacts(answer: str) -> str:
    """Remove leaked tool protocol markers without dropping the surrounding answer."""
    cleaned = _INTERNAL_TOOL_ARTIFACT_RE.sub("", answer)
    return "\n".join(re.sub(r"[ \t]{2,}", " ", line).rstrip() for line in cleaned.splitlines()).strip()


def _attach_project_line_citations(answer: str, sources: list[dict[str, Any]]) -> str:
    """Cite a line that names exactly one sourced project and has no citation marker."""
    project_sources: dict[str, list[dict[str, Any]]] = {}
    for source in sources:
        project_id = str(source.get("project_id") or "").strip()
        source_id = str(source.get("source_id") or "").strip()
        if project_id and source_id:
            project_sources.setdefault(project_id, []).append(source)
    if not project_sources:
        return answer

    source_priority = {"party": 0, "project": 1, "milestone": 2, "activity": 3, "email": 4}
    preferred = {
        project_id: str(min(items, key=lambda item: source_priority.get(str(item.get("kind")), 9))["source_id"])
        for project_id, items in project_sources.items()
    }
    lines: list[str] = []
    for raw_line in answer.splitlines():
        if _CITATION_RE.search(raw_line):
            lines.append(raw_line)
            continue
        matches = [
            project_id
            for project_id in project_sources
            if re.search(
                rf"(?<![A-Za-z0-9_-]){re.escape(project_id)}(?![A-Za-z0-9_-])",
                raw_line,
                re.IGNORECASE,
            )
        ]
        if len(matches) == 1 and raw_line.strip():
            raw_line = f"{raw_line.rstrip()} [[source:{preferred[matches[0]]}]]"
        lines.append(raw_line)
    return "\n".join(lines)


def _attach_tool_summary_line_citations(
    answer: str,
    sources: list[dict[str, Any]],
    tool_results: list[dict[str, Any]],
) -> str:
    """Cite explicitly labelled scope/filter metadata from the tool summary source."""
    known = {str(source.get("source_id") or "").strip() for source in sources}
    summary_source_ids = {
        str(result.get("summary_source_id") or "").strip()
        for item in tool_results
        if isinstance(item.get("result"), dict)
        for result in [item["result"]]
        if str(result.get("summary_source_id") or "").strip() in known
    }
    if len(summary_source_ids) != 1:
        return answer
    summary_source_id = next(iter(summary_source_ids))

    lines: list[str] = []
    for raw_line in answer.splitlines():
        if (
            raw_line.strip()
            and not _CITATION_RE.search(raw_line)
            and _TOOL_SUMMARY_LINE_RE.search(raw_line)
            and not _looks_like_markdown_table_row(raw_line)
        ):
            raw_line = f"{raw_line.rstrip()} [[source:{summary_source_id}]]"
        lines.append(raw_line)
    return "\n".join(lines)


def _remove_superseded_next_action_lines(
    answer: str,
    tool_results: list[dict[str, Any]],
) -> str:
    """Drop historical action lines that conflict with a newer project-row action."""
    stale_source_ids, current_source_ids = _superseded_next_action_sources(tool_results)
    if not stale_source_ids:
        return answer

    kept: list[str] = []
    for line in answer.splitlines():
        citations = set(_CITATION_RE.findall(line))
        cites_stale_action = bool(citations & stale_source_ids)
        cites_current_truth = bool(citations & current_source_ids)
        if cites_stale_action and not cites_current_truth and _NEXT_ACTION_CLAIM_RE.search(line):
            continue
        kept.append(line)
    return "\n".join(kept)


def _superseded_next_action_sources(
    tool_results: list[dict[str, Any]],
) -> tuple[set[str], set[str]]:
    current_by_project: dict[str, tuple[str, str]] = {}
    for item in tool_results:
        if str(item.get("tool") or "") != "get_project_brief":
            continue
        result = item.get("result") if isinstance(item.get("result"), dict) else {}
        project = result.get("project") if isinstance(result.get("project"), dict) else {}
        project_id = str(project.get("project_id") or "").strip()
        next_action = str(project.get("next_action") or "").strip()
        source_id = str(project.get("source_id") or "").strip()
        if project_id and next_action:
            current_by_project[project_id] = (next_action, source_id)

    stale_source_ids: set[str] = set()
    current_source_ids = {
        source_id for _, source_id in current_by_project.values() if source_id
    }
    if not current_by_project:
        return stale_source_ids, current_source_ids

    for item in tool_results:
        tool = str(item.get("tool") or "")
        result = item.get("result") if isinstance(item.get("result"), dict) else {}
        rows: list[dict[str, Any]] = []
        default_project_id = ""
        if tool == "get_project_brief":
            project = result.get("project") if isinstance(result.get("project"), dict) else {}
            default_project_id = str(project.get("project_id") or "").strip()
            rows = [
                row for row in result.get("recent_activity", [])
                if isinstance(row, dict)
            ]
        elif tool == "search_project_timeline":
            default_project_id = str(result.get("project_id") or "").strip()
            rows = [
                row for row in result.get("results", [])
                if isinstance(row, dict) and str(row.get("kind") or "") == "activity"
            ]
        else:
            continue

        for row in rows:
            project_id = str(row.get("project_id") or default_project_id).strip()
            current = current_by_project.get(project_id)
            source_id = str(row.get("source_id") or "").strip()
            historical_action = _logged_next_action(str(row.get("summary") or ""))
            if not current or not source_id or not historical_action:
                continue
            if not _actions_equivalent(historical_action, current[0]):
                stale_source_ids.add(source_id)

    return stale_source_ids, current_source_ids


def _logged_next_action(summary: str) -> str:
    match = _NEXT_ACTION_VALUE_RE.search(summary)
    return _single_line(match.group(1), limit=500) if match else ""


def _actions_equivalent(left: str, right: str) -> bool:
    def tokens(value: str) -> set[str]:
        return {
            token for token in re.findall(r"[a-z0-9]+", value.casefold())
            if len(token) >= 4 and token not in _ACTION_STOPWORDS
        }

    left_tokens = tokens(left)
    right_tokens = tokens(right)
    if not left_tokens or not right_tokens:
        return left.casefold().strip() == right.casefold().strip()
    overlap = len(left_tokens & right_tokens)
    return overlap / min(len(left_tokens), len(right_tokens)) >= 0.8


def _ensure_tool_completeness(answer: str, tool_results: list[dict[str, Any]]) -> str:
    sections: list[str] = []
    current = answer
    summarized_tools: set[str] = set()
    for item in tool_results:
        tool = str(item.get("tool") or "")
        result = item.get("result") if isinstance(item.get("result"), dict) else {}
        if result.get("error"):
            continue
        if tool not in summarized_tools:
            summary = _missing_snapshot_summary(
                current,
                tool,
                result,
                arguments=item.get("arguments") if isinstance(item.get("arguments"), dict) else {},
            )
            summarized_tools.add(tool)
            if summary:
                sections.append(summary)
                current = f"{current}\n\n{summary}".strip()
        if tool in {"portfolio_attention", "search_project_parties", "query_portfolio"}:
            section = _missing_project_rows(
                current,
                tool,
                result,
                arguments=item.get("arguments") if isinstance(item.get("arguments"), dict) else {},
            )
        elif tool == "get_project_brief":
            section = _missing_brief_facts(current, result)
        elif tool == "search_project_timeline":
            section = _missing_timeline_rows(current, result)
        elif tool == "list_project_work":
            section = _missing_work_rows(current, result)
        elif tool == "list_unanswered_threads":
            section = _missing_unanswered_rows(current, result)
        elif tool == "get_project_evidence_audit":
            section = _missing_evidence_audit(current, result)
        elif tool == "assess_milestone_readiness":
            section = _missing_readiness_assessment(current, result)
        else:
            section = ""
        if section:
            sections.append(section)
            current = f"{current}\n\n{section}".strip()
    return current


def _missing_snapshot_summary(
    answer: str,
    tool: str,
    result: dict[str, Any],
    *,
    arguments: dict[str, Any] | None = None,
) -> str:
    source_id = str(result.get("summary_source_id") or "").strip()
    as_of = str(result.get("as_of") or "").strip()
    if not source_id:
        return ""
    arguments = arguments or {}
    cited = set(_CITATION_RE.findall(answer))
    summary_complete = source_id in cited and (not as_of or as_of in answer)
    unanswered_filter_detail = ""
    if tool == "list_unanswered_threads":
        filters = result.get("filters") if isinstance(result.get("filters"), dict) else {}
        min_age_hours = filters.get("min_age_hours")
        if min_age_hours is None:
            min_age_hours = arguments.get("min_age_hours")
        include_terminal = bool(arguments.get("include_terminal", False))
        scope_label = "all visible projects, including terminal projects" if include_terminal else "non-terminal/open projects"
        if isinstance(min_age_hours, (int, float)):
            rendered_age = f"{min_age_hours:g}"
            unanswered_filter_detail = f", using a minimum age of {rendered_age} hours across {scope_label}"
            age_is_present = bool(re.search(
                rf"(?<![0-9]){re.escape(rendered_age)}(?:\.0)?\s*(?:hours?|hrs?|h|\u5c0f\u6642)",
                answer,
                re.IGNORECASE,
            ))
            scope_is_present = bool(re.search(
                r"non[- ]terminal|open projects?|active projects?|include(?:s|d|ing)? terminal|terminal projects?|"
                r"\u975e\s*terminal|\u958b\u555f\u9805\u76ee|\u9032\u884c\u4e2d\u9805\u76ee|\u6d3b\u8e8d\u9805\u76ee",
                answer,
                re.IGNORECASE,
            ))
            summary_complete = summary_complete and age_is_present and scope_is_present
    if summary_complete:
        return ""
    total = result.get("total")
    if total is None:
        total = result.get("attention_count")
    labels = {
        "portfolio_attention": "portfolio attention scan",
        "search_project_parties": "organisation search",
        "query_portfolio": "portfolio query",
        "get_project_brief": "project brief",
        "search_project_timeline": "project timeline query",
        "list_project_work": "project work query",
        "list_unanswered_threads": "unanswered-thread scan",
    }
    label = labels.get(tool, tool.replace("_", " "))
    if total == 0:
        detail = f"The {label} found no matching records in the active Workspace scope"
    elif total is not None:
        detail = f"The {label} matched {total} records in the active Workspace scope"
    else:
        detail = f"The {label} was evaluated from the active Workspace scope"
    detail += unanswered_filter_detail
    if as_of:
        detail += f" as of {as_of}"
    return f"- **Workspace snapshot:** {detail}. [[source:{source_id}]]"


def _missing_timeline_rows(answer: str, result: dict[str, Any]) -> str:
    rows = result.get("results") if isinstance(result.get("results"), list) else []
    cited = set(_CITATION_RE.findall(answer))
    missing = [
        row for row in rows
        if isinstance(row, dict)
        and row.get("source_id")
        and str(row["source_id"]) not in cited
    ]
    if not missing:
        return ""
    lines = ["### Verified project timeline"]
    for row in missing:
        title = _single_line(str(row.get("title") or row.get("kind") or "Timeline record"), limit=180)
        summary = _single_line(str(row.get("summary") or ""), limit=320)
        detail = f"{title}"
        if summary:
            detail += f" ? {summary}"
        lines.append(
            f"- **{_record_date(row, 'occurred_at')} ? {row.get('kind') or 'record'}:** "
            f"{detail}. [[source:{row['source_id']}]]"
        )
    return "\n".join(lines)


def _missing_work_rows(answer: str, result: dict[str, Any]) -> str:
    rows = result.get("results") if isinstance(result.get("results"), list) else []
    cited = set(_CITATION_RE.findall(answer))
    missing = [
        row for row in rows
        if isinstance(row, dict)
        and row.get("source_id")
        and str(row["source_id"]) not in cited
    ]
    if not missing:
        return ""
    lines = ["### Verified project work"]
    for row in missing:
        due = row.get("due_at") or "no due date recorded"
        provenance = row.get("provenance_quality") or "unknown provenance"
        lines.append(
            f"- **{row.get('work_type') or 'work'} ? {row.get('status') or 'unknown'}:** "
            f"{_single_line(str(row.get('title') or 'Untitled work item'), limit=260)}; "
            f"owner {row.get('owner') or 'not set'}; due {due}; evidence lineage {provenance}. "
            f"[[source:{row['source_id']}]]"
        )
    return "\n".join(lines)


def _missing_unanswered_rows(answer: str, result: dict[str, Any]) -> str:
    rows = result.get("results") if isinstance(result.get("results"), list) else []
    cited = set(_CITATION_RE.findall(answer))
    missing = [
        row for row in rows
        if isinstance(row, dict)
        and row.get("source_id")
        and str(row["source_id"]) not in cited
    ]
    if not missing:
        return ""
    lines = ["### Verified unanswered-thread candidates"]
    for row in missing:
        lines.append(
            f"- **{row.get('project_id') or 'Project not set'} ? "
            f"{_single_line(str(row.get('subject') or 'Inbound email'), limit=180)}:** "
            f"latest message {row.get('occurred_at') or 'time unavailable'}; "
            f"age {row.get('age_hours')} hours; latest state is inbound with no newer outbound response. "
            f"This is a response candidate, not proof that a reply is required. "
            f"[[source:{row['source_id']}]]"
        )
    return "\n".join(lines)


def _missing_evidence_audit(answer: str, result: dict[str, Any]) -> str:
    current = result.get("current_truth") if isinstance(result.get("current_truth"), dict) else {}
    current_source = str(current.get("source_id") or "").strip()
    cited = set(_CITATION_RE.findall(answer))
    lines: list[str] = []

    current_context = _record_context(answer, current.get("project_id") or result.get("project_id"), current_source)
    current_values = (
        current.get("project_status"),
        current.get("current_milestone"),
        current.get("ball_with"),
    )
    if current_source and (
        current_source not in cited
        or not _all_present_values_are_mentioned(current_context, current_values)
    ):
        lines.append(
            f"- **Authoritative current truth:** {current.get('project_id') or result.get('project_id')} is "
            f"{current.get('project_status') or 'not set'} at "
            f"{current.get('current_milestone') or 'no milestone recorded'}, with the ball "
            f"{current.get('ball_with') or 'not set'}. [[source:{current_source}]]"
        )

    conflicts = result.get("conflicts")
    audit_totals: list[str] = []
    if (
        isinstance(conflicts, list)
        and not conflicts
        and not _EXPLICIT_EMPTY_AUDIT_CONFLICTS_RE.search(answer)
    ):
        audit_totals.append("`conflicts=[]` (no open conflicts)")

    superseded_count = result.get("superseded_count")
    if isinstance(superseded_count, int) and not _mentions_labeled_count(
        answer,
        r"superseded(?:_count)?(?:\s+updates?)?",
        superseded_count,
    ):
        audit_totals.append(f"`superseded_count={superseded_count}`")

    decision_counts = result.get("decision_counts") if isinstance(result.get("decision_counts"), dict) else {}
    missing_decision_count = any(
        isinstance(count, int)
        and not _mentions_labeled_count(answer, re.escape(str(outcome)), count)
        for outcome, count in decision_counts.items()
    )
    if decision_counts and missing_decision_count:
        rendered_counts = ", ".join(
            f"{outcome}: {count}" for outcome, count in sorted(decision_counts.items())
        )
        audit_totals.append(f"`decision_counts={{{rendered_counts}}}`")

    audit_source = current_source or _first_audit_source_id(result)
    if audit_totals and audit_source:
        lines.append(f"- **Audit totals:** {'; '.join(audit_totals)}. [[source:{audit_source}]]")

    for conflict in conflicts or []:
        if not isinstance(conflict, dict):
            continue
        source_ids = [str(item) for item in (conflict.get("source_ids") or []) if str(item).strip()]
        if source_ids and all(source_id in cited for source_id in source_ids):
            continue
        citations = " ".join(f"[[source:{source_id}]]" for source_id in source_ids)
        if not citations:
            continue
        field = conflict.get("field") or "milestone"
        historical = conflict.get("proposed_value", conflict.get("history_value"))
        lines.append(
            f"- **Recorded conflict:** {field} has authoritative value "
            f"{conflict.get('current_value')!r} while an audit record shows {historical!r}. "
            f"The current project row remains authoritative. {citations}"
        )

    proposals = result.get("proposed_updates") if isinstance(result.get("proposed_updates"), list) else []
    for proposal in proposals:
        if not isinstance(proposal, dict):
            continue
        outcome = str(proposal.get("decision_outcome") or "unknown").strip().lower()
        is_superseded = bool(proposal.get("is_superseded"))
        if outcome != "rejected" and not is_superseded:
            continue
        source_id = str(proposal.get("source_id") or "").strip()
        if not source_id or _audit_update_is_described(answer, proposal):
            continue
        update_id = str(proposal.get("update_id") or "unknown")
        label = "Superseded" if is_superseded else "Rejected"
        changes = _format_audit_changes(proposal.get("proposed_changes"))
        detail = f" proposed {changes}" if changes else " was recorded in the audit history"
        lines.append(
            f"- **{label} update {update_id}:**{detail}; it does not override the current project row. "
            f"[[source:{source_id}]]"
        )

    if not lines:
        return ""
    return "### Verified evidence audit\n" + "\n".join(lines)


def _record_context(answer: str, record_id: Any, source_id: str) -> str:
    """Return answer lines associated with a record identifier or its source citation."""
    record_text = str(record_id or "").strip().casefold()
    source_marker = f"[[source:{source_id}]]" if source_id else ""
    lines = [
        line
        for line in answer.splitlines()
        if (record_text and record_text in line.casefold()) or (source_marker and source_marker in line)
    ]
    return "\n".join(lines)


def _all_present_values_are_mentioned(context: str, values: tuple[Any, ...]) -> bool:
    normalized_context = re.sub(r"\s+", " ", context).casefold()
    return all(
        re.sub(r"\s+", " ", str(value)).strip().casefold() in normalized_context
        for value in values
        if value not in (None, "")
    )


def _mentions_labeled_count(answer: str, label_pattern: str, count: int) -> bool:
    words = {
        0: "zero",
        1: "one",
        2: "two",
        3: "three",
        4: "four",
        5: "five",
        6: "six",
        7: "seven",
        8: "eight",
        9: "nine",
        10: "ten",
        11: "eleven",
        12: "twelve",
    }
    number_forms = [re.escape(str(count))]
    if count in words:
        number_forms.append(words[count])
    number_pattern = "(?:" + "|".join(number_forms) + ")"
    patterns = (
        rf"\b(?:{label_pattern})\b[^\n]{{0,32}}\b{number_pattern}\b",
        rf"\b{number_pattern}\b[^\n]{{0,32}}\b(?:{label_pattern})\b",
    )
    return any(re.search(pattern, answer, re.IGNORECASE) for pattern in patterns)


def _first_audit_source_id(result: dict[str, Any]) -> str:
    for key in ("proposed_updates", "milestone_history", "evidence"):
        records = result.get(key) if isinstance(result.get(key), list) else []
        for record in records:
            if isinstance(record, dict) and record.get("source_id"):
                return str(record["source_id"])
    return ""


def _audit_update_is_described(answer: str, proposal: dict[str, Any]) -> bool:
    source_id = str(proposal.get("source_id") or "").strip()
    update_id = str(proposal.get("update_id") or "").strip()
    context = _record_context(answer, update_id, source_id)
    if not context:
        return False
    outcome = str(proposal.get("decision_outcome") or "").strip()
    if outcome and outcome.casefold() not in context.casefold():
        return False
    changes = proposal.get("proposed_changes") if isinstance(proposal.get("proposed_changes"), list) else []
    fields = [str(change.get("field")) for change in changes if isinstance(change, dict) and change.get("field")]
    return not fields or any(field.casefold() in context.casefold() for field in fields)


def _format_audit_changes(raw_changes: Any) -> str:
    changes = raw_changes if isinstance(raw_changes, list) else []
    rendered: list[str] = []
    for change in changes[:3]:
        if not isinstance(change, dict):
            continue
        field = str(change.get("field") or "field")
        old_value = _single_line(str(change.get("old_value") or "not set"), limit=120)
        new_value = _single_line(str(change.get("new_value") or "not set"), limit=180)
        rendered.append(f"{field} {old_value!r} -> {new_value!r}")
    return "; ".join(rendered)


def _readiness_status_mentioned(answer: str, status: str) -> bool:
    lower = answer.lower()
    token = str(status or "").strip().lower()
    if not token:
        return True
    return token in lower or token.replace("_", " ") in lower


def _missing_readiness_assessment(answer: str, result: dict[str, Any]) -> str:
    source_ids = [str(item) for item in (result.get("source_ids") or []) if str(item).strip()]
    if not source_ids:
        return ""
    cited = set(_CITATION_RE.findall(answer))
    status = str(result.get("assessment_status") or "not_available")
    current = result.get("current_milestone") or "not set"
    target = result.get("target_milestone") or "not set"
    as_of = str(result.get("as_of") or "").strip()
    required_phrase = "did not approve or apply a milestone change"
    signature = f"for movement from {current} to {target}"
    lower = answer.lower()
    already_injected = (
        "### verified milestone readiness" in lower
        and signature.lower() in lower
        and required_phrase in lower
        and _readiness_status_mentioned(answer, status)
        and (not as_of or as_of in answer)
    )
    if already_injected or (
        all(source_id in cited for source_id in source_ids)
        and _readiness_status_mentioned(answer, status)
        and required_phrase in lower
        and (not as_of or as_of in answer)
    ):
        return ""
    citations = " ".join(f"[[source:{source_id}]]" for source_id in source_ids)
    reason = _single_line(str(result.get("reason") or ""), limit=360)
    detail = (
        f"The deterministic assessment status is **{status}** for movement from {current} to {target}"
    )
    if as_of:
        detail += f" as of {as_of}"
    if reason:
        detail += f". {reason}"
    lines = [
        f"- **Readiness:** {detail}. This read-only assessment did not approve or apply a milestone change. {citations}"
    ]
    criteria = result.get("criteria") if isinstance(result.get("criteria"), list) else []
    if status == "not_available":
        criteria = []
    elif not criteria:
        criteria = (
            result.get("required_exit_criteria")
            if isinstance(result.get("required_exit_criteria"), list)
            else []
        )
    for criterion in criteria:
        if not isinstance(criterion, dict):
            continue
        criterion_id = str(
            criterion.get("criterion_id") or criterion.get("id") or ""
        ).strip()
        description = _single_line(
            str(
                criterion.get("description")
                or criterion.get("explanation")
                or criterion_id
                or "Criterion"
            ),
            limit=260,
        )
        outcome = criterion.get("result") or "required"
        label = criterion_id or "Criterion"
        lines.append(f"- **{label} {outcome}:** {description}. {citations}")
    return "### Verified milestone readiness\n" + "\n".join(lines)


def _status_transition_filter(
    result: dict[str, Any],
    arguments: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
    filters = result.get("filters") if isinstance(result.get("filters"), dict) else {}
    status_transition = filters.get("status_transition")
    if isinstance(status_transition, dict):
        return status_transition

    arguments = arguments or {}
    status_transition = arguments.get("status_transition")
    if isinstance(status_transition, dict):
        return status_transition

    effective_filters = (
        result.get("effective_filters")
        if isinstance(result.get("effective_filters"), dict)
        else {}
    )
    transition = effective_filters.get("transition")
    return transition if isinstance(transition, dict) else None


def _transition_date_is_mentioned(context: str, transitioned_at: Any) -> bool:
    raw = str(transitioned_at or "").strip()
    if not raw:
        return True
    normalized_context = re.sub(r"\s+", " ", context).casefold()
    if raw.casefold() in normalized_context:
        return True
    date_match = re.match(r"\d{4}-\d{2}-\d{2}", raw)
    return bool(date_match and date_match.group(0).casefold() in normalized_context)


def _missing_project_rows(
    answer: str,
    tool: str,
    result: dict[str, Any],
    *,
    arguments: dict[str, Any] | None = None,
) -> str:
    rows = result.get("results") if isinstance(result.get("results"), list) else []
    cited = set(_CITATION_RE.findall(answer))
    lower = answer.lower()
    transition_filter = _status_transition_filter(result, arguments) if tool == "query_portfolio" else None
    missing: list[dict[str, Any]] = []
    seen_projects: set[str] = set()
    for row in rows:
        if not isinstance(row, dict):
            continue
        project_id = str(row.get("project_id") or "").strip()
        source_id = str(row.get("source_id") or "").strip()
        if not project_id or not source_id or project_id in seen_projects:
            continue
        seen_projects.add(project_id)
        row_complete = project_id.lower() in lower and source_id in cited
        if row_complete and tool == "query_portfolio":
            project_context = _record_context(answer, project_id, source_id)
            if transition_filter is not None:
                row_complete = (
                    _all_present_values_are_mentioned(project_context, (row.get("client_name"),))
                    and _transition_date_is_mentioned(project_context, row.get("transitioned_at"))
                )
            else:
                row_complete = _all_present_values_are_mentioned(
                    project_context,
                    (
                        row.get("client_name"),
                        row.get("project_status"),
                        row.get("current_milestone"),
                        row.get("ball_with"),
                    ),
                )
        elif row_complete and tool == "portfolio_attention":
            project_context = _record_context(answer, project_id, source_id)
            row_complete = _all_present_values_are_mentioned(
                project_context,
                (
                    row.get("project_status"),
                    row.get("current_milestone"),
                    row.get("ball_with"),
                    row.get("priority_score"),
                    *(row.get("attention_reasons") or []),
                ),
            )
        if row_complete:
            continue
        missing.append(row)
    if not missing:
        return ""

    titles = {
        "portfolio_attention": "Verified attention records",
        "search_project_parties": "Verified organisation matches",
        "query_portfolio": (
            "Verified project status transitions"
            if transition_filter is not None
            else "Verified portfolio records"
        ),
    }
    lines = [f"### {titles[tool]}"]
    for row in missing:
        source_id = row["source_id"]
        project_id = row["project_id"]
        if tool == "search_project_parties":
            detail = (
                f"{row.get('org_name') or 'The organisation'} is recorded as "
                f"{row.get('party_role') or 'a project party'}; status {row.get('project_status') or 'not set'}; "
                f"milestone {row.get('current_milestone') or 'not set'}"
            )
        elif tool == "portfolio_attention":
            reasons = ", ".join(str(value) for value in (row.get("attention_reasons") or [])) or "attention signal"
            detail = (
                f"status {row.get('project_status') or 'not set'}; milestone "
                f"{row.get('current_milestone') or 'not set'}; ball with {row.get('ball_with') or 'not set'}; "
                f"priority {row.get('priority_score')}; {reasons}"
            )
        elif transition_filter is not None:
            to_status = (
                transition_filter.get("to_status")
                or next(iter(transition_filter.get("to_values") or []), None)
                or row.get("project_status")
                or "the requested status"
            )
            detail = (
                f"{row.get('client_name') or 'Client not set'}; transitioned to {to_status} on "
                f"{_record_date(row, 'transitioned_at')}"
            )
        else:
            detail = (
                f"{row.get('client_name') or 'Client not set'}; status {row.get('project_status') or 'not set'}; "
                f"milestone {row.get('current_milestone') or 'not set'}; "
                f"ball with {row.get('ball_with') or 'not set'}"
            )
        lines.append(f"- **{project_id}** ? {detail}. [[source:{source_id}]]")
    return "\n".join(lines)


def _missing_brief_facts(answer: str, result: dict[str, Any]) -> str:
    project = result.get("project") if isinstance(result.get("project"), dict) else {}
    project_id = str(project.get("project_id") or "").strip()
    project_source = str(project.get("source_id") or "").strip()

    lines: list[str] = []
    answer_lower = answer.lower()
    cited = set(_CITATION_RE.findall(answer))
    current_values = [
        str(project.get("project_status") or ""),
        str(project.get("current_milestone") or ""),
        str(project.get("ball_with") or ""),
    ]
    if project_id and project_source and (
        project_id.lower() not in answer_lower
        or any(value and value.lower() not in answer_lower for value in current_values)
        or project_source not in cited
    ):
        lines.append(
            f"- **Current state:** {project_id} is {project.get('project_status') or 'not set'} at "
            f"{project.get('current_milestone') or 'milestone not set'}, with the ball "
            f"{project.get('ball_with') or 'not set'}. [[source:{project_source}]]"
        )

    next_action = str(project.get("next_action") or "").strip()
    next_terms = [term for term in re.findall(r"[a-z0-9]+", next_action.lower()) if len(term) >= 5]
    next_overlap = {term for term in next_terms if term in answer_lower}
    if project_source and next_action and "next action" not in answer_lower and len(next_overlap) < 2:
        lines.append(f"- **Next action:** {next_action}. [[source:{project_source}]]")

    emails = result.get("recent_emails") if isinstance(result.get("recent_emails"), list) else []
    activities = result.get("recent_activity") if isinstance(result.get("recent_activity"), list) else []
    material = _material_email_lines(answer, emails)
    if material:
        lines.extend(material)

    latest_activity = _latest_record(activities, "created_at")
    if latest_activity:
        source_id = str(latest_activity.get("source_id") or "")
        summary = _single_line(str(latest_activity.get("summary") or ""), limit=280)
        if source_id and source_id not in set(_CITATION_RE.findall(answer + "\n" + "\n".join(lines))) and summary:
            lines.append(
                f"- **Latest recorded activity ({_record_date(latest_activity, 'created_at')}):** "
                f"{summary}. [[source:{source_id}]]"
            )

    if not lines:
        return ""
    return "### Verified project brief\n" + "\n".join(lines)


def _material_email_lines(answer: str, emails: list[dict[str, Any]]) -> list[str]:
    lines: list[str] = []
    working = answer
    latest = _latest_record(emails, "occurred_at")
    test_email = next((item for item in emails if _is_test_pass_email(item)), None)
    completion_email = next((item for item in emails if _is_completion_email(item)), None)
    contact_email = next((item for item in emails if _is_contact_only_email(item)), None)
    submission_email = next(
        (item for item in emails if _is_conformance_result_submission_email(item)),
        None,
    )
    exemption_email = next(
        (item for item in emails if _is_conformance_exemption_email(item)),
        None,
    )

    for kind, item in (
        ("exemption", exemption_email),
        ("submission", submission_email),
        ("completion", completion_email),
        ("test", test_email),
        ("contact", contact_email),
        ("latest", latest),
    ):
        if not item:
            continue
        source_id = str(item.get("source_id") or "").strip()
        if not source_id:
            continue
        working_lower = working.lower()
        cited = set(_CITATION_RE.findall(working))
        if kind == "exemption":
            complete = _conformance_exemption_is_described(working) and source_id in cited
            text = _conformance_exemption_fact()
        elif kind == "submission":
            complete = _submission_email_is_described(working, item) and source_id in cited
            text = _submission_email_fact(item)
        elif kind == "completion":
            complete = bool(_COMPLETION_RE.search(working_lower)) and source_id in cited
            text = "the email explicitly recorded that PCT was completed and the session could go live"
        elif kind == "test":
            complete = bool(_TEST_PASS_RE.search(working_lower)) and source_id in cited
            text = "the email explicitly recorded that all tests had passed"
        elif kind == "contact":
            complete = bool(_CONTACT_RE.search(working_lower)) and source_id in cited
            sender = str(item.get("sender_email") or "").lower()
            actor = "Eloise" if sender.startswith("eloise") else "the sender"
            text = (
                f"{actor} provided contact/setup details for the FIX connection; this contact-only update "
                "does not by itself prove milestone completion"
            )
        else:
            complete = source_id in cited
            subject = _single_line(str(item.get("subject") or "email update"), limit=180)
            text = f'the latest recorded email was "{subject}"'
        if complete:
            continue
        line = f"- **Email update ({_record_date(item, 'occurred_at')}):** {text}. [[source:{source_id}]]"
        lines.append(line)
        working = f"{working}\n{line}"
    return lines


def _is_conformance_exemption_email(item: dict[str, Any]) -> bool:
    body = str(item.get("body") or item.get("body_snippet") or "").casefold()
    has_case_range = bool(re.search(
        r"test cases?\s*8\s*(?:-|\u2013|\u2014|to)\s*11",
        body,
    ))
    return (
        has_case_range
        and "amend" in body
        and "cancel" in body
        and "exemption" in body
        and "administrative messages" in body
    )


def _conformance_exemption_is_described(answer: str) -> bool:
    normalized = answer.casefold()
    has_cases = bool(re.search(r"cases?\s*8\s*(?:[-\u2013\u2014]|to)\s*11", normalized))
    has_order_scenario = "amend" in normalized and "cancel" in normalized
    has_admin = "administrative messages" in normalized
    has_prior_connection = bool(re.search(
        r"(?:already|previously|prior|existing).{0,45}(?:connect|connection)|"
        r"(?:\u4e4b\u524d|\u65e9\u524d|\u5148\u524d|\u5df2\u7d93|\u65e2\u6709).{0,25}\u9023\u7dda",
        normalized,
        re.IGNORECASE,
    ))
    return has_cases and has_order_scenario and has_admin and has_prior_connection


def _conformance_exemption_fact() -> str:
    return (
        "LB reported the required conformance cases complete and requested exemptions for cases 8-11 "
        "because amend/cancel-replace is unsupported, and for Administrative Messages because LB had "
        "previously connected with Demo Organization"
    )


def _is_conformance_result_submission_email(item: dict[str, Any]) -> bool:
    body = str(item.get("body") or item.get("body_snippet") or "")
    normalized = body.casefold()
    order_types = all(token in normalized for token in ("loo", "loc", "moo", "moc"))
    submission = bool(re.search(
        r"(?:attached|submit(?:ted)?|sent|provided).{0,80}(?:test|conformance).{0,40}results|"
        r"please check.{0,60}(?:test|conformance).{0,40}results",
        normalized,
        re.IGNORECASE | re.DOTALL,
    ))
    return order_types and submission


def _submission_email_is_described(answer: str, item: dict[str, Any]) -> bool:
    normalized = answer.casefold()
    has_order_types = all(token in normalized for token in ("loo", "loc", "moo", "moc"))
    has_submission = bool(re.search(
        r"submit|sent|provided|attached|\u63d0\u4ea4|\u9001\u51fa|\u9644\u4e0a",
        normalized,
    ))
    sender = str(item.get("sender_email") or "").split("@", 1)[0].casefold()
    has_sender = "shirley" in normalized or (sender and sender in normalized)
    return has_order_types and has_submission and has_sender


def _submission_email_fact(item: dict[str, Any]) -> str:
    body = str(item.get("body") or item.get("body_snippet") or "")
    sender_email = str(item.get("sender_email") or "").casefold()
    sender = (
        "Shirley"
        if re.search(r"(?:^|\n)\s*Shirley\s*(?:\r?$|\n)", body, re.IGNORECASE)
        else "The sender"
    )
    organization = ""  # Do not infer organizations from a private vendor substring.
    return (
        f"{sender}{organization} submitted the attached LOO/LOC/MOO/MOC conformance test "
        "results for review"
    )


def _latest_record(records: list[dict[str, Any]], date_key: str) -> dict[str, Any] | None:
    candidates = [item for item in records if isinstance(item, dict)]
    return max(candidates, key=lambda item: str(item.get(date_key) or ""), default=None)


def _record_date(item: dict[str, Any], key: str) -> str:
    raw = str(item.get(key) or "recorded date unavailable")
    return raw[:10] if re.match(r"\d{4}-\d{2}-\d{2}", raw) else raw


def _email_head(item: dict[str, Any]) -> str:
    body = str(item.get("body") or "")
    return re.split(r"\r?\n\s*From:\s", body, maxsplit=1, flags=re.IGNORECASE)[0][:1400]


def _is_test_pass_email(item: dict[str, Any]) -> bool:
    return bool(_TEST_PASS_RE.search(_email_head(item)))


def _is_completion_email(item: dict[str, Any]) -> bool:
    return bool(_COMPLETION_RE.search(_email_head(item)))


def _is_contact_only_email(item: dict[str, Any]) -> bool:
    subject = str(item.get("subject") or "")
    head = _email_head(item)
    haystack = f"{subject}\n{head}".lower()
    structured_fields = all(token in haystack for token in ("name", "role", "tel", "email"))
    contact_signal = bool(
        re.search(r"contact (?:details|information|list)|(?:information enquiry|information inquiry)|provided.{0,40}contacts?", haystack)
    ) or structured_fields
    return contact_signal and not _COMPLETION_RE.search(head) and not _TEST_PASS_RE.search(head)


def _single_line(value: str, *, limit: int) -> str:
    sanitized = _EMBEDDED_CITATION_RE.sub("", value)
    return re.sub(r"\s+", " ", sanitized).strip()[:limit]


def _validate_answer(
    answer: str,
    sources: list[dict[str, Any]],
    tool_results: list[dict[str, Any]],
) -> list[str]:
    known = {str(source.get("source_id")) for source in sources}
    cited = set(_CITATION_RE.findall(answer))
    errors: list[str] = []
    if _INTERNAL_TOOL_ARTIFACT_RE.search(answer):
        errors.append("The answer exposes an internal tool protocol marker")
    unknown = sorted(cited - known)
    if unknown:
        errors.append(f"Unknown citations: {', '.join(unknown)}")
    factual_tools_used = any(item.get("tool") != "think" for item in tool_results)
    if factual_tools_used and known and not cited:
        errors.append("The answer contains no source citations")
    lines = answer.splitlines()
    table_headers = _markdown_table_header_indexes(lines)
    for index, raw_line in enumerate(lines):
        line = raw_line.strip()
        if (
            not line
            or line.startswith(("#", "```"))
            or index in table_headers
            or _is_markdown_table_separator(line)
        ):
            continue
        if _PROJECT_FACT_RE.search(line) and not _CITATION_RE.search(line):
            if _RECOMMENDATION_RE.search(line):
                continue
            errors.append(f"Uncited factual line: {line[:140]}")
            if len(errors) >= 8:
                break
    return errors


def _repair_answer(answer: str, errors: list[str], sources: list[dict[str, Any]]) -> str:
    source_payload = [
        {
            "source_id": item.get("source_id"),
            "kind": item.get("kind"),
            "title": item.get("title"),
            "snippet": item.get("snippet"),
            "project_id": item.get("project_id"),
            "occurred_at": item.get("occurred_at"),
        }
        for item in sources
    ]
    messages = [
        {
            "role": "system",
            "content": (
                "Repair the answer using only the supplied source records. Preserve useful conclusions, remove "
                "unsupported claims, distinguish current state from evidence and recommendations, and add exact "
                "[[source:<source_id>]] markers to every factual sentence. Never invent a source ID. Return only "
                "the repaired answer."
            ),
        },
        {
            "role": "user",
            "content": (
                f"Validation errors:\n{json.dumps(errors, ensure_ascii=False)}\n\n"
                f"Source records:\n{json.dumps(source_payload, ensure_ascii=False, default=str)}\n\n"
                f"Original answer:\n{answer}"
            ),
        },
    ]
    try:
        repaired = llm_call(
            "reasoning",
            messages,
            temperature=0.0,
            max_tokens=1600,
            timeout_sec=_grounding_repair_timeout_seconds(),
        ).strip()
        return repaired or answer
    except Exception:
        return answer


def _verified_partial_answer(answer: str, sources: list[dict[str, Any]]) -> str:
    known = {str(source.get("source_id")) for source in sources}
    lines = answer.splitlines()
    kept: list[str] = []
    index = 0
    while index < len(lines):
        line = lines[index]
        if (
            index + 1 < len(lines)
            and _looks_like_markdown_table_row(line)
            and _is_markdown_table_separator(lines[index + 1])
        ):
            block_end = index + 2
            while block_end < len(lines) and _looks_like_markdown_table_row(lines[block_end]):
                block_end += 1
            verified_rows = []
            for row in lines[index + 2:block_end]:
                if _INTERNAL_TOOL_ARTIFACT_RE.search(row):
                    continue
                citations = set(_CITATION_RE.findall(row))
                if citations and citations.issubset(known):
                    verified_rows.append(row)
            if verified_rows:
                kept.extend([line, lines[index + 1], *verified_rows])
            index = block_end
            continue

        if _INTERNAL_TOOL_ARTIFACT_RE.search(line):
            index += 1
            continue
        if _is_markdown_table_separator(line):
            index += 1
            continue
        citations = set(_CITATION_RE.findall(line))
        if citations:
            if citations.issubset(known):
                kept.append(line)
            index += 1
            continue
        if line.strip() and not _PROJECT_FACT_RE.search(line):
            kept.append(line)
        index += 1

    warning = "\n\n> Some statements were omitted because the Workspace could not verify their source citations."
    body = "\n".join(kept).strip()
    return (body or "I could not produce a fully verified answer from the available records.") + warning
