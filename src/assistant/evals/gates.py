"""Deterministic gates and final scoring for Workspace eval runs."""

from __future__ import annotations

import re
from collections import Counter
from copy import deepcopy
from typing import Any

from src.models.tool_arg_constraints import constraint_passes
from src.tools.registry import TOOL_BY_NAME

from .models import (
    CandidateObservation,
    ConstraintOperator,
    EvalCase,
    EvidenceBundle,
    FailureClass,
    GateResult,
    GoldenSpec,
    JudgeResult,
    RequiredClaimResult,
    SemanticJudgeOutput,
    ToolPlan,
)

_CITATION_RE = re.compile(r"\[\[source:([A-Za-z0-9][A-Za-z0-9_.:-]*)\]\]")
_INTERNAL_MARKER_RE = re.compile(r"\bmonads0x[a-z0-9_]+\b", re.IGNORECASE)
_FALSE_WRITE_GUARD_RE = re.compile(
    r"claims? (?:success|completion).*no write tools?|no write tools? (?:were )?called|"
    r"聲稱已完成操作.*工具紀錄未能確認|未能確認.*write tools?",
    re.IGNORECASE | re.DOTALL,
)
_HISTORICAL_CONTEXT_RE = re.compile(
    r"\b(?:earlier|previously|historical|history|superseded|overridden|cancelled|"
    r"rejected|former|used to)\b|"
    r"(?:\u65e9\u524d|\u4e4b\u524d|\u904e\u53bb|\u66fe\u7d93|\u66fe|"
    r"\u7576\u6642|\u5176\u5f8c|\u6b77\u53f2|\u5df2\u53d6\u6d88|"
    r"\u5df2\u62d2\u7d55|\u539f\u5148|\u539f\u672c)",
    re.IGNORECASE,
)
_CONDITIONAL_CONTEXT_RE = re.compile(
    r"\b(?:need(?:s|ed)? to|recommend(?:ed|ation)?|suggest(?:ed|ion)?|should|could|"
    r"before|verify|pending|awaiting|requires? verification|not yet|candidate|"
    r"proposed|must first)\b|"
    r"(?:\u9700\u8981|\u5efa\u8b70|\u61c9\u8a72|\u53ef\u4ee5|\u5f85|"
    r"\u7b49\u5f85|\u5c1a\u672a|\u9084\u672a|\u672a\u78ba\u8a8d|\u9700\u5148|"
    r"\u5fc5\u9808\u5148|\u5019\u9078|\u64ec\u8b70)",
    re.IGNORECASE,
)
_NEGATED_PREDICATE_RE = re.compile(
    r"\b(?:not|never|isn't|wasn't|hasn't|haven't|doesn't|didn't)\b[^.;:]{0,50}"
    r"\b(?:completed|closed|resolved|fixed|launched|live|production|go-live)\b|"
    r"(?:\u5c1a\u672a|\u9084\u672a|\u672a|\u4e26\u672a|\u6c92\u6709|"
    r"\u4e0d\u662f|\u672a\u80fd)[^\u3002\uff1b;\uff1a:]{0,35}"
    r"(?:\u5b8c\u6210|\u95dc\u9589|\u89e3\u6c7a|\u4fee\u5fa9|"
    r"\u4e0a\u7dda|\u6295\u7522|go-live)",
    re.IGNORECASE,
)
_NEGATED_OPEN_RE = re.compile(
    r"\b(?:no|none|zero)\b[^.;:\n]{0,70}"
    r"\b(?:still\s+open|open|in progress|active projects?)\b|"
    r"(?:\u6c92\u6709|\u7121|\u4e26\u7121|\u96f6\u500b)[^\u3002\uff1b;\uff1a:\n]{0,40}"
    r"(?:\u4ecd\u958b\u555f|\u5c1a\u672a\u5b8c\u6210|\u9032\u884c\u4e2d|\u6d3b\u8e8d\u9805\u76ee)",
    re.IGNORECASE,
)
_GENERIC_COMPLETION_PATTERNS = frozenset({"completed"})


def _gate(
    gate_id: str,
    passed: bool,
    failure_class: FailureClass | None,
    reason: str,
    *,
    blocking: bool = True,
    details: dict[str, Any] | None = None,
) -> GateResult:
    return GateResult(
        gate_id=gate_id,
        passed=bool(passed),
        blocking=blocking,
        failure_class=None if passed else failure_class,
        reason=reason,
        details=details or {},
    )


def _arguments_with_schema_defaults(tool_name: str, arguments: dict[str, Any]) -> dict[str, Any]:
    """Return effective public arguments after applying JSON Schema defaults."""
    effective = dict(arguments)
    contract = TOOL_BY_NAME.get(tool_name)
    schema = contract.input_schema if contract is not None else {}
    properties = schema.get("properties") if isinstance(schema, dict) else {}
    if not isinstance(properties, dict):
        return effective
    for key, property_schema in properties.items():
        if key in effective or not isinstance(property_schema, dict) or "default" not in property_schema:
            continue
        effective[key] = deepcopy(property_schema["default"])
    return effective


def _trace_projection(
    trace: list[dict[str, Any]],
    *,
    apply_schema_defaults: bool = False,
) -> dict[str, Any]:
    calls: dict[str, dict[str, list[Any]]] = {}
    for item in trace:
        tool = str(item.get("tool") or "")
        arguments = item.get("arguments") if isinstance(item.get("arguments"), dict) else {}
        if apply_schema_defaults:
            arguments = _arguments_with_schema_defaults(tool, arguments)
        target = calls.setdefault(tool, {})
        for key, value in arguments.items():
            target.setdefault(str(key), []).append(value)
    return {"calls": calls}


def _primary_plan_passes(case: EvalCase, trace: list[dict[str, Any]]) -> tuple[bool, bool, dict[str, Any]]:
    calls = [item for item in trace if item.get("tool") == case.expected_tool]
    selection = bool(calls)
    if not selection:
        return False, False, {"expected_tool": case.expected_tool, "actual_tools": [item.get("tool") for item in trace]}
    if not case.expected_args_constraints:
        return True, True, {"matching_calls": len(calls)}
    args_pass = any(
        all(
            constraint_passes(
                item.get("arguments") or {}
                if constraint.operator in {ConstraintOperator.ABSENT, ConstraintOperator.PRESENT}
                else _arguments_with_schema_defaults(case.expected_tool, item.get("arguments") or {}),
                constraint,
            )
            for constraint in case.expected_args_constraints
        )
        for item in calls
    )
    return True, args_pass, {"matching_calls": len(calls)}


def _alternative_plan_passes(plan: ToolPlan, trace: list[dict[str, Any]]) -> tuple[bool, bool]:
    counts = Counter(str(item.get("tool") or "") for item in trace)
    selection = all(counts[name] >= required for name, required in plan.required_tool_counts.items())
    if not selection:
        return False, False
    raw_projection = _trace_projection(trace)
    effective_projection = _trace_projection(trace, apply_schema_defaults=True)
    args_pass = all(
        constraint_passes(
            raw_projection
            if constraint.operator in {ConstraintOperator.ABSENT, ConstraintOperator.PRESENT}
            else effective_projection,
            constraint,
        )
        for constraint in plan.args_constraints
    )
    return True, args_pass


def evaluate_deterministic_gates(
    case: EvalCase,
    evidence: EvidenceBundle,
    golden: GoldenSpec,
    candidate: CandidateObservation,
) -> list[GateResult]:
    """Run hard checks that the semantic judge cannot override."""
    trace = candidate.tool_trace
    actual_tools = candidate.actual_tools or [str(item.get("tool") or "") for item in trace]
    primary_selection, primary_args, primary_details = _primary_plan_passes(case, trace)
    plan_results = [_alternative_plan_passes(plan, trace) for plan in case.alternative_tool_plans]
    tool_selection = primary_selection or any(selection for selection, _ in plan_results)
    tool_args = primary_args or any(selection and args_ok for selection, args_ok in plan_results)

    selected_names = {case.expected_tool}
    for plan in case.alternative_tool_plans:
        selected_names.update(plan.required_tool_counts)
    selected_traces = [item for item in trace if item.get("tool") in selected_names]
    tool_execution = bool(selected_traces) and all(
        isinstance(item.get("result"), dict) and not item["result"].get("error")
        for item in selected_traces
    )

    candidate_source_ids = {
        str(item.get("source_id"))
        for item in candidate.source_parts
        if item.get("source_id")
    }
    cited_source_ids = set(_CITATION_RE.findall(candidate.answer))
    unknown_citations = sorted(cited_source_ids - candidate_source_ids)
    scope_leaks: list[str] = []
    if case.scope_mode == "projects":
        allowed = set(case.project_ids)
        for item in candidate.source_parts:
            project_id = item.get("project_id")
            if project_id and str(project_id) not in allowed:
                scope_leaks.append(str(project_id))

    no_error = candidate.error is None and bool(candidate.answer.strip())
    no_marker = not _INTERNAL_MARKER_RE.search(candidate.answer)
    no_false_write_guard = not _FALSE_WRITE_GUARD_RE.search(candidate.answer)
    citations_resolve = bool(cited_source_ids) and not unknown_citations
    validation_clean = not candidate.validation_errors

    return [
        _gate(
            "candidate_completed",
            no_error,
            FailureClass.INFRA_OR_MODEL,
            candidate.error or "candidate returned a non-empty answer",
        ),
        _gate(
            "tool_selection",
            tool_selection,
            FailureClass.TOOL_SELECTION,
            "an accepted tool plan was selected" if tool_selection else "no accepted tool plan was selected",
            details={"actual_tools": actual_tools, **primary_details},
        ),
        _gate(
            "tool_args_scope",
            tool_args and not scope_leaks,
            FailureClass.TOOL_ARGS_SCOPE,
            "tool arguments and source scope satisfy the case"
            if tool_args and not scope_leaks
            else "tool arguments or source scope do not satisfy the case",
            details={"scope_leaks": sorted(set(scope_leaks))},
        ),
        _gate(
            "tool_execution",
            tool_execution,
            FailureClass.TOOL_EXECUTION,
            "selected domain tools completed" if tool_execution else "a selected domain tool failed or has no result",
        ),
        _gate(
            "citations_resolve",
            citations_resolve,
            FailureClass.GROUNDING_CITATION,
            "all answer citations resolve" if citations_resolve else "answer has missing or unknown citations",
            details={"cited": sorted(cited_source_ids), "unknown": unknown_citations},
        ),
        _gate(
            "grounding_validation_clean",
            validation_clean,
            FailureClass.GROUNDING_CITATION,
            "runtime grounding validation is clean" if validation_clean else "runtime reported grounding errors",
            details={"validation_errors": candidate.validation_errors},
        ),
        _gate(
            "no_internal_marker_leakage",
            no_marker,
            FailureClass.ANSWER_FORMAT,
            "no internal marker leaked" if no_marker else "internal model/tool marker leaked",
        ),
        _gate(
            "no_readonly_write_guard_false_positive",
            no_false_write_guard,
            FailureClass.SAFETY_GUARD_FALSE_POSITIVE,
            "no false write-success warning" if no_false_write_guard else "read-only answer was replaced or prefixed by a false write guard",
        ),
    ]


def _deterministic_forbidden_violations(
    case: EvalCase,
    golden: GoldenSpec,
    candidate: CandidateObservation,
) -> list[str]:
    violations: list[str] = []
    for forbidden in golden.forbidden_claims:
        for pattern in forbidden.patterns:
            try:
                regex = re.compile(pattern, re.IGNORECASE)
            except re.error:
                continue
            matched_violation = False
            for unit in _answer_units(candidate.answer):
                for match in regex.finditer(unit):
                    if _contextual_non_violation(
                        unit,
                        match,
                        pattern=pattern,
                        case=case,
                    ):
                        continue
                    violations.append(forbidden.claim_id)
                    matched_violation = True
                    break
                if matched_violation:
                    break
            if matched_violation:
                break
    return violations


def _filter_semantic_forbidden_violations(
    case: EvalCase,
    golden: GoldenSpec,
    candidate: CandidateObservation,
    semantic_violations: list[str],
) -> list[str]:
    """Drop judge violations when every literal pattern match is clearly non-assertive."""
    claims = {claim.claim_id: claim for claim in golden.forbidden_claims}
    filtered: list[str] = []
    for claim_id in semantic_violations:
        claim = claims.get(claim_id)
        if claim is None or not claim.patterns:
            filtered.append(claim_id)
            continue
        matched_any = False
        violating_match = False
        for pattern in claim.patterns:
            try:
                regex = re.compile(pattern, re.IGNORECASE)
            except re.error:
                continue
            for unit in _answer_units(candidate.answer):
                for match in regex.finditer(unit):
                    matched_any = True
                    if not _contextual_non_violation(unit, match, pattern=pattern, case=case):
                        violating_match = True
                        break
                if violating_match:
                    break
            if violating_match:
                break
        if not matched_any or violating_match:
            filtered.append(claim_id)
    return filtered


def _answer_units(answer: str) -> list[str]:
    """Return line-sized assertion units so regex wildcards cannot span the answer."""
    return [line.strip() for line in str(answer or "").splitlines() if line.strip()]


def _contextual_non_violation(
    unit: str,
    match: re.Match[str],
    *,
    pattern: str,
    case: EvalCase,
) -> bool:
    """Ignore historical, conditional, negated, and non-project completion mentions."""
    prefix = unit[:match.start()]
    matched = match.group(0)
    context = unit[max(0, match.start() - 240):min(len(unit), match.end() + 160)]
    before_or_match = f"{prefix[-240:]} {matched}"
    suffix = unit[match.end():min(len(unit), match.end() + 100)]

    if re.match(
        r"[^?\uFF1F]{0,12}[?\uFF1F].{0,45}(?:\b(?:no|not|false)\b|\u5426|\u975e|\u5514\u4fc2)",
        suffix,
        re.IGNORECASE,
    ):
        return True
    if _HISTORICAL_CONTEXT_RE.search(before_or_match):
        return True
    if _CONDITIONAL_CONTEXT_RE.search(context):
        return True
    if _NEGATED_PREDICATE_RE.search(context):
        return True
    if _NEGATED_OPEN_RE.search(context):
        return True

    normalized_pattern = pattern.strip().casefold()
    if normalized_pattern in {"completed", "testing complete", "all tests passed"}:
        partial_phase = bool(re.search(
            r"\b(?:M[1-7](?:_[A-Za-z0-9]+)?|UAT|conformance|milestone|phase)\b",
            context,
            re.IGNORECASE,
        ))
        historical_transition = bool(re.search(
            r"\b(?:changed|moved|advanced|progressed)\s+from\b|"
            r"\bfrom\s+M[1-7].{0,60}\bto\s+M[1-8]\b|"
            r"\u7531.{0,45}(?:\u9032\u81f3|\u63a8\u9032\u81f3|\u8f49\u81f3)",
            context,
            re.IGNORECASE,
        ))
        if normalized_pattern == "testing complete" and partial_phase:
            return True
        if partial_phase and historical_transition:
            return True

    if normalized_pattern in _GENERIC_COMPLETION_PATTERNS:
        return not _is_current_project_completion_assertion(unit, match, case)
    return False


def _is_current_project_completion_assertion(
    unit: str,
    match: re.Match[str],
    case: EvalCase,
) -> bool:
    """Treat generic ``completed`` as forbidden only for current project status."""
    normalized = unit.strip()
    identifies_project = any(
        re.search(rf"\b{re.escape(project_id)}\b", normalized, re.IGNORECASE)
        for project_id in case.project_ids
        if project_id
    )
    status_language = bool(re.search(
        r"\b(?:project|status|current state|currently|is|remains|marked)\b|"
        r"(?:\u5c08\u6848|\u9805\u76ee|\u72c0\u614b|\u76ee\u524d|"
        r"\u7576\u524d|\u73fe\u6642|\u6a19\u8a18\u70ba)",
        normalized,
        re.IGNORECASE,
    ))
    local_prefix = normalized[max(0, match.start() - 70):match.start()]
    non_project_object = bool(re.search(
        r"\b(?:work item|follow-?up|task|test(?:ing)?|order|review|conformance|PCT|"
        r"milestone|activity|request|email)\b|"
        r"(?:\u5de5\u4f5c\u9805\u76ee|\u8ddf\u9032|\u4efb\u52d9|\u6e2c\u8a66|"
        r"\u8a02\u55ae|\u5be9\u67e5|\u4e00\u81f4\u6027|\u91cc\u7a0b\u7891|"
        r"\u6d3b\u52d5|\u8acb\u6c42|\u96fb\u90f5|\u90f5\u4ef6)",
        local_prefix,
        re.IGNORECASE,
    ))
    return identifies_project and status_language and not non_project_object


def finalize_judge_result(
    case: EvalCase,
    golden: GoldenSpec,
    candidate: CandidateObservation,
    gates: list[GateResult],
    semantic: SemanticJudgeOutput,
    *,
    judge_model: str | None = None,
    judge_error: str | None = None,
) -> JudgeResult:
    """Compute the authoritative score; blocking gates always veto a pass."""
    gate_map = {gate.gate_id: gate for gate in gates}
    tool_selection_pass = gate_map["tool_selection"].passed and gate_map["tool_execution"].passed
    tool_args_pass = gate_map["tool_args_scope"].passed
    grounding_gate_ids = (
        "citations_resolve",
        "grounding_validation_clean",
        "no_internal_marker_leakage",
    )
    grounding_pass = all(gate_map[item].passed for item in grounding_gate_ids)

    expected_claim_ids = {claim.claim_id for claim in golden.required_claims}
    result_by_id = {item.claim_id: item for item in semantic.required_claim_results}
    normalized_claims: list[RequiredClaimResult] = []
    judge_invalid = False
    for claim in golden.required_claims:
        result = result_by_id.get(claim.claim_id)
        if result is None:
            judge_invalid = True
            result = RequiredClaimResult(
                claim_id=claim.claim_id,
                passed=False,
                reason="semantic judge omitted this required claim",
            )
        normalized_claims.append(result)
    if set(result_by_id) - expected_claim_ids and not judge_error:
        judge_invalid = True

    violations = list(dict.fromkeys([
        *_filter_semantic_forbidden_violations(
            case,
            golden,
            candidate,
            semantic.forbidden_claim_violations,
        ),
        *_deterministic_forbidden_violations(case, golden, candidate),
    ]))
    claim_ratio = (
        sum(1 for item in normalized_claims if item.passed) / len(normalized_claims)
        if normalized_claims
        else 1.0
    )
    score = 0.0
    score += 15 if tool_selection_pass else 0
    score += 15 if tool_args_pass else 0
    score += 25 if grounding_pass else 0
    score += 35 * claim_ratio
    score += 10 if not violations else 0
    final_score = max(0, min(100, round(score)))

    critical_claims_pass = all(
        result_by_id.get(claim.claim_id) is not None and result_by_id[claim.claim_id].passed
        for claim in golden.required_claims
        if claim.critical
    )
    critical_forbidden_ids = {claim.claim_id for claim in golden.forbidden_claims if claim.critical}
    critical_forbidden_clean = not (set(violations) & critical_forbidden_ids)
    blocking_gates_pass = all(gate.passed for gate in gates if gate.blocking)
    overall_pass = (
        final_score >= case.pass_threshold
        and blocking_gates_pass
        and critical_claims_pass
        and critical_forbidden_clean
        and not judge_invalid
        and not judge_error
    )

    failure_classes = [gate.failure_class for gate in gates if not gate.passed and gate.failure_class]
    if any(not item.passed for item in normalized_claims) and not judge_error:
        failure_classes.append(FailureClass.REQUIRED_CLAIM_MISSING)
    if violations:
        failure_classes.append(FailureClass.FORBIDDEN_OVERCLAIM)
    if judge_invalid and not judge_error:
        failure_classes.append(FailureClass.JUDGE_INVALID)
    if judge_error:
        failure_classes.append(FailureClass.INFRA_OR_MODEL)

    citation_issues = list(semantic.citation_issues)
    citation_gate = gate_map["citations_resolve"]
    if not citation_gate.passed:
        citation_issues.append(citation_gate.reason)

    return JudgeResult(
        case_id=case.case_id,
        repetition=candidate.repetition,
        overall_pass=overall_pass,
        score_0_to_100=final_score,
        tool_selection_pass=tool_selection_pass,
        tool_args_pass=tool_args_pass,
        grounding_pass=grounding_pass,
        required_claim_results=normalized_claims,
        forbidden_claim_violations=violations,
        citation_issues=list(dict.fromkeys(citation_issues)),
        main_differences=semantic.main_differences,
        recommended_agent_fix=semantic.recommended_agent_fix,
        deterministic_gates=gates,
        failure_classes=list(dict.fromkeys(failure_classes)),
        judge_model=judge_model,
        judge_error=judge_error,
        confidence=semantic.confidence,
    )
