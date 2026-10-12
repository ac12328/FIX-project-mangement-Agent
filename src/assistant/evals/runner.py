"""Evidence collection and repeated Candidate Workspace Agent evaluation."""

from __future__ import annotations

import re
import sqlite3
import time
import uuid
from collections.abc import Callable, Iterable
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .artifacts import content_hash, lint_artifact_set, redact_value, stamp_content_hash, utc_now
from .gates import evaluate_deterministic_gates, finalize_judge_result
from .llm_roles import StructuredCall, judge_candidate
from .models import (
    CandidateObservation,
    CaseRunResult,
    EvalCase,
    EvalRunResult,
    EvidenceBundle,
    EvidenceSource,
    FailureClass,
    GoldenSpec,
    JudgeResult,
    RequiredClaimResult,
    SemanticJudgeOutput,
    SuiteManifest,
)

CandidateProvider = Callable[[sqlite3.Connection, EvalCase, int], CandidateObservation]


def clone_sqlite_database(path: Path) -> sqlite3.Connection:
    """Clone a SQLite database into memory so eval threads never touch production."""
    source_uri = f"{path.resolve().as_uri()}?mode=ro"
    source = sqlite3.connect(source_uri, uri=True, timeout=30)
    target = sqlite3.connect(":memory:", check_same_thread=False)
    try:
        source.backup(target)
    finally:
        source.close()
    target.row_factory = sqlite3.Row
    target.execute("PRAGMA foreign_keys = ON")
    return target


def _case_requests_all_projects(case: EvalCase) -> bool:
    """Recognize explicit all-project intent without relying on model-generated arguments."""
    if bool(case.evidence_tool_args.get("include_terminal")):
        return True
    normalized_tags = {
        re.sub(r"[^a-z0-9]+", "-", tag.lower()).strip("-")
        for tag in case.tags
    }
    if normalized_tags & {
        "all-projects",
        "all-project",
        "entire-portfolio",
        "full-portfolio",
        "include-terminal",
        "portfolio-complete",
    }:
        return True
    question = " ".join(case.question.lower().split())
    return bool(re.search(
        r"\b(?:all(?: the)? projects?|every project|entire portfolio|full portfolio|check all)\b",
        question,
    ))


def _merge_evidence_pages(pages: list[dict[str, Any]]) -> dict[str, Any]:
    """Merge deterministic cursor pages while retaining per-page summary provenance."""
    if not pages:
        return {}
    merged = dict(pages[0])
    rows: list[Any] = []
    summary_ids: list[str] = []
    seen_rows: set[str] = set()
    for page in pages:
        summary_id = str(page.get("summary_source_id") or "").strip()
        if summary_id and summary_id not in summary_ids:
            summary_ids.append(summary_id)
        page_rows = page.get("results") if isinstance(page.get("results"), list) else []
        for row in page_rows:
            identity = content_hash(row)
            if identity in seen_rows:
                continue
            seen_rows.add(identity)
            rows.append(row)
    if any(isinstance(page.get("results"), list) for page in pages):
        merged["results"] = rows
        merged["result_count"] = len(rows)
    merged["pages_fetched"] = len(pages)
    merged["page_summary_source_ids"] = summary_ids
    merged["next_cursor"] = None
    if summary_ids:
        merged["summary_source_id"] = summary_ids[0]
    return merged


def collect_evidence(
    conn: sqlite3.Connection,
    case: EvalCase,
    *,
    max_pages: int = 100,
) -> EvidenceBundle:
    """Execute and fully paginate a deterministic read-only evidence tool."""
    from src.tools.assistant_workspace_handlers import ASSISTANT_WORKSPACE_TOOL_HANDLERS

    handler = ASSISTANT_WORKSPACE_TOOL_HANDLERS.get(case.expected_tool)
    if handler is None or case.expected_tool == "web_search":
        raise ValueError(f"Unsupported deterministic evidence tool: {case.expected_tool}")
    sources: list[dict[str, Any]] = []
    all_projects_requested = _case_requests_all_projects(case)
    base_arguments = {
        **case.evidence_tool_args,
        "_assistant_scope_mode": case.scope_mode,
        "_assistant_scope_project_ids": list(case.project_ids),
        "_assistant_source_sink": sources,
        "_assistant_web_enabled": False,
        "_assistant_as_of": case.as_of.isoformat(),
        "_assistant_all_projects_requested": all_projects_requested,
    }
    pages: list[dict[str, Any]] = []
    cursor = str(base_arguments.get("cursor") or "").strip() or None
    seen_cursors: set[str] = set()
    while True:
        if cursor:
            if cursor in seen_cursors:
                raise RuntimeError(f"{case.expected_tool} returned a repeated cursor")
            seen_cursors.add(cursor)
        arguments = dict(base_arguments)
        if cursor:
            arguments["cursor"] = cursor
        elif "cursor" in arguments:
            arguments.pop("cursor")
        result = handler(conn, arguments)
        if not isinstance(result, dict) or result.get("error"):
            raise RuntimeError(str(result.get("error") if isinstance(result, dict) else "invalid tool result"))
        pages.append(result)
        next_cursor = str(result.get("next_cursor") or "").strip() or None
        if not next_cursor:
            break
        if len(pages) >= max_pages:
            raise RuntimeError(
                f"{case.expected_tool} pagination exceeded the {max_pages}-page safety limit"
            )
        cursor = next_cursor

    combined_result = _merge_evidence_pages(pages)
    sanitized, redactions = redact_value({"tool_result": combined_result, "sources": sources})
    parsed_sources = [EvidenceSource.model_validate(item) for item in sanitized["sources"]]
    eligible_source_times = [
        source.occurred_at
        for source in parsed_sources
        if source.occurred_at and source.occurred_at <= case.as_of
    ]
    data_watermark = max(eligible_source_times, default=case.as_of)
    captured_at = max(utc_now(), case.as_of.astimezone(timezone.utc), data_watermark.astimezone(timezone.utc))
    evidence = EvidenceBundle(
        case_id=case.case_id,
        case_version=case.version,
        captured_at=captured_at,
        data_watermark=data_watermark,
        as_of=case.as_of,
        tool_name=case.expected_tool,
        tool_args=case.evidence_tool_args,
        tool_result=sanitized["tool_result"],
        sources=parsed_sources,
        redactions=redactions,
    )
    return stamp_content_hash(evidence)


def run_workspace_candidate(
    conn: sqlite3.Connection,
    case: EvalCase,
    repetition: int,
) -> CandidateObservation:
    """Run the actual Candidate Agent with autonomy routing and no forced tools."""
    from src.assistant.agent_runtime import run_workspace_agent
    from src.repositories.assistant_repo import create_thread

    thread = create_thread(
        conn,
        owner_user_id="ai-workspace-eval",
        title=f"Eval: {case.case_id} #{repetition}",
        scope_mode=case.scope_mode,
        project_ids=list(case.project_ids),
        web_enabled=False,
    )
    progress: list[dict[str, Any]] = []

    def on_progress(step: str, status: str, detail: str | None) -> None:
        progress.append({"step": step, "status": status, "detail": detail})

    started = time.monotonic()
    try:
        result = run_workspace_agent(
            conn,
            owner_user_id="ai-workspace-eval",
            thread_id=thread["thread_id"],
            message=case.question,
            project_ids=list(case.project_ids),
            scope_mode=case.scope_mode,
            web_enabled=False,
            progress_cb=on_progress,
            as_of=case.as_of,
            routing_mode="autonomy_eval",
        )
    except Exception as exc:
        return CandidateObservation(
            case_id=case.case_id,
            repetition=repetition,
            elapsed_seconds=time.monotonic() - started,
            error=f"{type(exc).__name__}: {exc}",
            progress=progress,
        )
    source_parts = [part for part in result.parts if part.get("type") == "source"]
    return CandidateObservation(
        case_id=case.case_id,
        repetition=repetition,
        answer=result.text,
        actual_tools=list(result.tool_names),
        tool_trace=list(result.tool_results),
        source_parts=source_parts,
        validation_errors=list(result.validation_errors),
        progress=progress,
        elapsed_seconds=time.monotonic() - started,
        agent_trace_id=getattr(result, "trace_id", None),
        agent_run_id=getattr(result, "run_id", None),
        agent_turn_id=getattr(result, "turn_id", None),
    )


def _failed_semantic(golden: GoldenSpec, reason: str) -> SemanticJudgeOutput:
    return SemanticJudgeOutput(
        required_claim_results=[
            RequiredClaimResult(claim_id=claim.claim_id, passed=False, reason=reason)
            for claim in golden.required_claims
        ],
        main_differences=[reason],
        recommended_agent_fix="Restore a successful Candidate Agent and judge run before interpreting quality failures.",
        confidence=1.0,
    )


def evaluate_observation(
    case: EvalCase,
    evidence: EvidenceBundle,
    golden: GoldenSpec,
    candidate: CandidateObservation,
    *,
    structured_call: StructuredCall | None = None,
    judge_model_override: str | None = None,
) -> JudgeResult:
    gates = evaluate_deterministic_gates(case, evidence, golden, candidate)
    judge_error: str | None = None
    if candidate.error:
        semantic = _failed_semantic(golden, candidate.error)
        judge_model = None
    else:
        try:
            semantic, judge_model = judge_candidate(
                case,
                evidence,
                golden,
                candidate.model_dump(mode="json"),
                [gate.model_dump(mode="json") for gate in gates],
                structured_call=structured_call,
                model_override=judge_model_override,
            )
        except Exception as exc:
            judge_error = f"semantic judge failed: {type(exc).__name__}: {exc}"
            semantic = _failed_semantic(golden, judge_error)
            judge_model = None
        else:
            judge_error = None
    return finalize_judge_result(
        case,
        golden,
        candidate,
        gates,
        semantic,
        judge_model=judge_model,
        judge_error=judge_error if not candidate.error else None,
    )


def _critical_suite_failure(result: JudgeResult, golden: GoldenSpec) -> bool:
    critical_forbidden = {item.claim_id for item in golden.forbidden_claims if item.critical}
    if set(result.forbidden_claim_violations) & critical_forbidden:
        return True
    for gate in result.deterministic_gates:
        if gate.gate_id == "tool_args_scope" and gate.details.get("scope_leaks"):
            return True
    return FailureClass.FIXTURE_INVALID in result.failure_classes


def aggregate_case_results(
    case: EvalCase,
    evidence: EvidenceBundle,
    golden: GoldenSpec,
    candidates: list[CandidateObservation],
    repetitions: list[JudgeResult],
    *,
    minimum_passes: int,
    pass_threshold: int,
) -> CaseRunResult:
    pass_count = sum(1 for result in repetitions if result.overall_pass)
    mean_score = sum(result.score_0_to_100 for result in repetitions) / max(1, len(repetitions))
    critical_failure = any(_critical_suite_failure(result, golden) for result in repetitions)
    flaky = 0 < pass_count < len(repetitions)
    if flaky:
        for result in repetitions:
            if FailureClass.FLAKY not in result.failure_classes:
                result.failure_classes.append(FailureClass.FLAKY)
    overall = pass_count >= minimum_passes and mean_score >= pass_threshold and not critical_failure
    return CaseRunResult(
        case_id=case.case_id,
        question=case.question,
        pm_intent=case.pm_intent,
        pm_rationale=case.pm_rationale,
        mining_batch_id=case.mining_batch_id,
        source_signal_ids=case.source_signal_ids,
        question_review=(
            case.question_review.model_dump(mode="json")
            if case.question_review is not None
            else {}
        ),
        evidence=evidence.model_dump(mode="json"),
        golden=golden.model_dump(mode="json"),
        candidates=candidates,
        repetitions=repetitions,
        pass_count=pass_count,
        minimum_passes=minimum_passes,
        mean_score=round(mean_score, 2),
        overall_pass=overall,
        flaky=flaky,
    )


def run_suite(
    conn: sqlite3.Connection,
    manifest: SuiteManifest,
    artifacts: Iterable[tuple[EvalCase, EvidenceBundle, GoldenSpec]],
    *,
    candidate_provider: CandidateProvider = run_workspace_candidate,
    structured_call: StructuredCall | None = None,
    judge_model_override: str | None = None,
    mode: str = "live",
) -> EvalRunResult:
    """Run a suite. Frozen PR manifests use 1x; nightly core manifests use 3x."""
    started_at = utc_now()
    case_results: list[CaseRunResult] = []
    artifact_hashes: dict[str, str] = {}
    for case, evidence, golden in artifacts:
        issues = lint_artifact_set(case, evidence, golden, require_frozen=manifest.kind.value != "generated")
        if issues:
            reason = "; ".join(f"{issue.code}: {issue.message}" for issue in issues)
            failed = _failed_semantic(golden, reason)
            repetitions: list[JudgeResult] = []
            candidates: list[CandidateObservation] = []
            for repetition in range(1, manifest.repetitions + 1):
                candidate = CandidateObservation(case_id=case.case_id, repetition=repetition, error=reason)
                candidates.append(candidate)
                gates = evaluate_deterministic_gates(case, evidence, golden, candidate)
                result = finalize_judge_result(case, golden, candidate, gates, failed)
                if FailureClass.FIXTURE_INVALID not in result.failure_classes:
                    result.failure_classes.insert(0, FailureClass.FIXTURE_INVALID)
                repetitions.append(result)
        else:
            repetitions = []
            candidates = []
            for repetition in range(1, manifest.repetitions + 1):
                candidate = candidate_provider(conn, case, repetition)
                candidates.append(candidate)
                repetitions.append(evaluate_observation(
                    case,
                    evidence,
                    golden,
                    candidate,
                    structured_call=structured_call,
                    judge_model_override=judge_model_override,
                ))
        case_results.append(aggregate_case_results(
            case,
            evidence,
            golden,
            candidates,
            repetitions,
            minimum_passes=manifest.minimum_passes,
            pass_threshold=manifest.pass_threshold,
        ))
        artifact_hashes[case.case_id] = content_hash({
            "case": case,
            "evidence": evidence,
            "golden": golden,
        })

    passed = sum(1 for item in case_results if item.overall_pass)
    result = EvalRunResult(
        run_id=f"aweval_{uuid.uuid4().hex}",
        suite_id=manifest.suite_id,
        mode=mode,
        started_at=started_at,
        completed_at=utc_now(),
        cases=case_results,
        passed_cases=passed,
        total_cases=len(case_results),
        all_passed=passed == len(case_results),
        artifact_hashes=artifact_hashes,
    )
    return stamp_content_hash(result)


def observations_from_report(path: Path) -> dict[str, list[CandidateObservation]]:
    """Load either legacy autonomy reports or framework JSON observations."""
    import json

    payload = json.loads(path.read_text(encoding="utf-8"))
    cases = payload.get("cases", payload if isinstance(payload, list) else [])
    grouped: dict[str, list[CandidateObservation]] = {}
    for item in cases:
        case_id = str(item["case_id"])
        observation = CandidateObservation(
            case_id=case_id,
            repetition=len(grouped.get(case_id, [])) + 1,
            answer=str(item.get("answer") or ""),
            actual_tools=list(item.get("actual_tools") or []),
            tool_trace=list(item.get("tool_trace") or []),
            source_parts=list(item.get("source_parts") or item.get("sources") or []),
            validation_errors=list(item.get("validation_errors") or []),
            progress=list(item.get("progress") or []),
            elapsed_seconds=float(item.get("elapsed_seconds") or 0),
            error=item.get("error"),
            agent_trace_id=item.get("agent_trace_id") or item.get("trace_id"),
            agent_run_id=item.get("agent_run_id") or item.get("run_id"),
            agent_turn_id=item.get("agent_turn_id") or item.get("turn_id"),
        )
        grouped.setdefault(case_id, []).append(observation)
    return grouped
