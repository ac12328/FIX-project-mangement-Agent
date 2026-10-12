"""Deterministic tool-argument and evidence-to-answer trace replay."""

from __future__ import annotations

import re
import sqlite3
from collections.abc import Iterable, Mapping, Sequence
from copy import deepcopy
from pathlib import Path
from typing import Any

from src.agent.tool_evidence_eval_models import (
    ArgumentConstraintResult,
    ConstraintOperator,
    EvidenceClaimResult,
    ScopeViolation,
    ToolArgumentExpectationResult,
    ToolCallEvidence,
    ToolEvidenceCaseResult,
    ToolEvidenceEvalCase,
    ToolEvidenceObservation,
    ToolEvidenceSuiteResult,
)
from src.agent.tool_evidence_trace import (
    load_tool_evidence_events,
    load_tool_evidence_spool,
    observation_from_tool_evidence_events,
)
from src.models.tool_arg_constraints import constraint_passes
from src.tools.registry import TOOL_BY_NAME

_CITATION_RE = re.compile(r"\[\[source:([A-Za-z0-9][A-Za-z0-9_.:-]*)\]\]")
_MISSING = object()
_SUCCESS_STATUSES = frozenset({"ok", "completed", "succeeded", "success"})


def _rate(numerator: int | float, denominator: int | float, *, empty: float = 1.0) -> float:
    if not denominator:
        return empty
    return round(float(numerator) / float(denominator), 6)


def _resolve_path(value: Any, path: str) -> Any:
    current = value
    for segment in (item for item in path.split(".") if item):
        if isinstance(current, Mapping) and segment in current:
            current = current[segment]
        else:
            return _MISSING
    return current


def _effective_arguments(tool_name: str, arguments: dict[str, Any]) -> dict[str, Any]:
    effective = dict(arguments)
    contract = TOOL_BY_NAME.get(tool_name)
    schema = contract.input_schema if contract is not None else {}
    properties = schema.get("properties") if isinstance(schema, Mapping) else {}
    if not isinstance(properties, Mapping):
        return effective
    for key, property_schema in properties.items():
        if key not in effective and isinstance(property_schema, Mapping) and "default" in property_schema:
            effective[str(key)] = deepcopy(property_schema["default"])
    return effective


def _constraint_result(expectation_id: str, tool_name: str, call: ToolCallEvidence | None, constraint: Any) -> ArgumentConstraintResult:
    raw = call.arguments if call is not None and call.arguments is not None else {}
    checked = raw if constraint.operator in {ConstraintOperator.ABSENT, ConstraintOperator.PRESENT} else _effective_arguments(tool_name, raw)
    actual = _resolve_path(checked, constraint.path)
    return ArgumentConstraintResult(
        expectation_id=expectation_id,
        tool_name=tool_name,
        tool_call_id=call.tool_call_id if call is not None else None,
        path=constraint.path,
        operator=constraint.operator,
        expected=constraint.value,
        actual=None if actual is _MISSING else actual,
        actual_present=actual is not _MISSING,
        critical=constraint.critical,
        passed=constraint_passes(checked, constraint),
    )


def _answer_units(answer: str) -> tuple[str, ...]:
    return tuple(line.strip() for line in answer.splitlines() if line.strip())


def _contains_redaction(value: Any) -> bool:
    if isinstance(value, str):
        return value in {"[REDACTED]", "[REDACTED_REASONING]"}
    if isinstance(value, Mapping):
        return any(_contains_redaction(item) for item in value.values())
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
        return any(_contains_redaction(item) for item in value)
    return False


def evaluate_tool_evidence(case: ToolEvidenceEvalCase, observation: ToolEvidenceObservation) -> ToolEvidenceCaseResult:
    expectation_results: list[ToolArgumentExpectationResult] = []
    selected_calls: list[tuple[ToolCallEvidence, bool]] = []
    all_constraint_results: list[ArgumentConstraintResult] = []
    for expectation in case.argument_expectations:
        calls = [call for call in observation.tool_calls if call.tool_name == expectation.tool_name]
        evaluated = [
            tuple(_constraint_result(expectation.expectation_id, expectation.tool_name, call, constraint) for constraint in expectation.constraints)
            for call in calls
        ]
        matching_count = sum(all(result.passed for result in results if result.critical) for results in evaluated)
        ranked = sorted(
            zip(calls, evaluated),
            key=lambda item: (
                sum(result.passed for result in item[1] if result.critical),
                sum(result.passed for result in item[1]),
            ),
            reverse=True,
        )
        chosen = ranked[:expectation.min_matching_calls]
        selected_calls.extend((call, expectation.critical) for call, _ in chosen)
        chosen_results = [result for _call, results in chosen for result in results]
        for _ in range(expectation.min_matching_calls - len(chosen)):
            chosen_results.extend(
                _constraint_result(expectation.expectation_id, expectation.tool_name, None, constraint)
                for constraint in expectation.constraints
            )
        all_constraint_results.extend(chosen_results)
        within_max = expectation.max_total_calls is None or len(calls) <= expectation.max_total_calls
        passed = matching_count >= expectation.min_matching_calls and within_max
        expectation_results.append(ToolArgumentExpectationResult(
            expectation_id=expectation.expectation_id,
            tool_name=expectation.tool_name,
            call_count=len(calls),
            matching_call_count=matching_count,
            min_matching_calls=expectation.min_matching_calls,
            max_total_calls=expectation.max_total_calls,
            passed=passed,
            constraint_results=tuple(chosen_results),
        ))

    scope_violations: list[ScopeViolation] = []
    for rule in case.scope_rules:
        for call in (item for item in observation.tool_calls if item.tool_name == rule.tool_name):
            actual = _resolve_path(call.arguments or {}, rule.path)
            if actual is _MISSING:
                if not rule.allow_missing:
                    scope_violations.append(ScopeViolation(
                        rule_id=rule.rule_id,
                        tool_name=rule.tool_name,
                        tool_call_id=call.tool_call_id,
                        path=rule.path,
                        missing=True,
                        critical=rule.critical,
                    ))
                continue
            values = actual if isinstance(actual, Sequence) and not isinstance(actual, (str, bytes)) else (actual,)
            normalized = tuple(str(item) for item in values)
            disallowed = tuple(item for item in normalized if item not in set(rule.allowed_values))
            if disallowed:
                scope_violations.append(ScopeViolation(
                    rule_id=rule.rule_id,
                    tool_name=rule.tool_name,
                    tool_call_id=call.tool_call_id,
                    path=rule.path,
                    actual_values=normalized,
                    disallowed_values=disallowed,
                    critical=rule.critical,
                ))

    successful_results = sum(
        call.result_payload_available and call.status.casefold() in _SUCCESS_STATUSES
        for call, _critical in selected_calls
    )
    critical_successful_results = sum(
        call.result_payload_available and call.status.casefold() in _SUCCESS_STATUSES
        for call, critical in selected_calls
        if critical
    )
    required_results = sum(item.min_matching_calls for item in case.argument_expectations)
    critical_required_results = sum(
        item.min_matching_calls for item in case.argument_expectations if item.critical
    )
    available = set(observation.available_source_ids)
    cited = set(observation.cited_source_ids)
    unknown = tuple(sorted(cited - available))
    answer_units = _answer_units(observation.final_answer)
    claim_results: list[EvidenceClaimResult] = []
    citation_supported_count = 0
    for claim in case.required_claims:
        matching_units = tuple(
            unit for unit in answer_units
            if any(re.search(pattern, unit, re.IGNORECASE) for pattern in claim.answer_patterns)
        )
        support = set(claim.supporting_source_ids)
        evidence_available = bool(support & available)
        unit_citations = set()
        for unit in matching_units:
            unit_citations.update(_CITATION_RE.findall(unit))
        relevant_citations = cited if claim.citation_mode == "anywhere" else unit_citations
        cited_support = tuple(sorted(relevant_citations & support & available))
        if matching_units and cited_support:
            citation_supported_count += 1
        supported = bool(matching_units) and evidence_available and bool(cited_support)
        claim_results.append(EvidenceClaimResult(
            claim_id=claim.claim_id,
            matched=bool(matching_units),
            evidence_available=evidence_available,
            cited_supporting_source_ids=cited_support,
            missing_supporting_source_ids=tuple(sorted(support - available)),
            supported=supported,
            critical=claim.critical,
        ))

    forbidden_violations = tuple(
        claim.claim_id
        for claim in case.forbidden_claims
        if any(re.search(pattern, observation.final_answer, re.IGNORECASE) for pattern in claim.patterns)
    )
    critical_expectations = [
        result.passed for expectation, result in zip(case.argument_expectations, expectation_results)
        if expectation.critical
    ]
    critical_scope = [violation for violation in scope_violations if violation.critical]
    critical_claims = [result.supported for claim, result in zip(case.required_claims, claim_results) if claim.critical]
    critical_forbidden = {
        claim.claim_id for claim in case.forbidden_claims if claim.critical
    } & set(forbidden_violations)
    redacted_argument_evidence = any(
        result.critical
        and result.operator not in {ConstraintOperator.ABSENT, ConstraintOperator.PRESENT}
        and _contains_redaction(result.actual)
        for result in all_constraint_results
    )
    trace_sufficient = (
        not any(issue.severity == "error" for issue in observation.issues)
        and not redacted_argument_evidence
    )
    if case.require_final_answer:
        trace_sufficient = trace_sufficient and observation.final_answer_available
    arguments_pass = all(critical_expectations)
    scope_pass = not critical_scope
    tool_results_pass = (
        not case.require_successful_results
        or critical_successful_results == critical_required_results
    )
    citations_pass = (case.allow_unknown_citations or not unknown) and all(
        result.cited_supporting_source_ids
        for claim, result in zip(case.required_claims, claim_results)
        if claim.critical
    )
    claims_pass = all(critical_claims)
    forbidden_pass = not critical_forbidden

    constraint_pass_count = sum(result.passed for result in all_constraint_results)
    missing_required = sum(
        result.critical and not result.actual_present and result.operator != ConstraintOperator.ABSENT
        for result in all_constraint_results
    )
    supported_claims = sum(result.supported for result in claim_results)
    findings: list[str] = []
    for finding, failed in (
        ("insufficient_trace_evidence", not trace_sufficient),
        ("redacted_argument_evidence", redacted_argument_evidence),
        ("argument_constraint_failure", not arguments_pass),
        ("missing_required_argument", bool(missing_required)),
        ("scope_overreach", not scope_pass),
        ("tool_result_unavailable", not tool_results_pass),
        ("unknown_citation", not (case.allow_unknown_citations or not unknown)),
        ("unsupported_required_claim", not claims_pass),
        ("forbidden_claim", not forbidden_pass),
    ):
        if failed:
            findings.append(finding)

    passed = all((
        trace_sufficient,
        arguments_pass,
        scope_pass,
        tool_results_pass,
        citations_pass,
        claims_pass,
        forbidden_pass,
    ))
    return ToolEvidenceCaseResult(
        case_id=case.case_id,
        run_id=observation.run_id,
        argument_constraint_count=len(all_constraint_results),
        argument_constraint_pass_count=constraint_pass_count,
        argument_constraint_pass_rate=_rate(constraint_pass_count, len(all_constraint_results)),
        missing_required_argument_count=missing_required,
        expectation_pass_count=sum(result.passed for result in expectation_results),
        expectation_count=len(expectation_results),
        scope_overreach_count=len(scope_violations),
        successful_result_count=successful_results,
        required_result_count=required_results,
        tool_result_availability_rate=_rate(successful_results, required_results),
        cited_source_count=len(cited),
        known_cited_source_count=len(cited & available),
        unknown_citations=unknown,
        cited_source_coverage=_rate(citation_supported_count, len(case.required_claims)),
        required_claim_count=len(case.required_claims),
        supported_claim_count=supported_claims,
        required_claim_support_rate=_rate(supported_claims, len(case.required_claims)),
        unsupported_claim_ids=tuple(result.claim_id for result in claim_results if not result.supported),
        forbidden_claim_violations=forbidden_violations,
        trace_sufficient=trace_sufficient,
        arguments_pass=arguments_pass,
        scope_pass=scope_pass,
        tool_results_pass=tool_results_pass,
        citations_pass=citations_pass,
        claims_pass=claims_pass,
        forbidden_claims_pass=forbidden_pass,
        passed=passed,
        findings=tuple(findings),
        expectation_results=tuple(expectation_results),
        scope_violations=tuple(scope_violations),
        claim_results=tuple(claim_results),
        observation=observation,
    )


def replay_tool_evidence_events(case: ToolEvidenceEvalCase, events: Iterable[Any]) -> ToolEvidenceCaseResult:
    """Evaluate stored events without invoking a model or executing a tool."""

    return evaluate_tool_evidence(case, observation_from_tool_evidence_events(events))


def replay_tool_evidence_run(
    conn: sqlite3.Connection,
    case: ToolEvidenceEvalCase,
    run_id: str,
) -> ToolEvidenceCaseResult:
    return replay_tool_evidence_events(case, load_tool_evidence_events(conn, run_id))


def replay_tool_evidence_spool(
    path: str | Path,
    case: ToolEvidenceEvalCase,
    run_id: str,
) -> ToolEvidenceCaseResult:
    return replay_tool_evidence_events(case, load_tool_evidence_spool(path, run_id))


def aggregate_tool_evidence_results(results: Iterable[ToolEvidenceCaseResult]) -> ToolEvidenceSuiteResult:
    collected = tuple(results)
    case_count = len(collected)
    constraint_count = sum(result.argument_constraint_count for result in collected)
    required_results = sum(result.required_result_count for result in collected)
    required_claims = sum(result.required_claim_count for result in collected)
    citation_supported = sum(
        bool(claim.cited_supporting_source_ids)
        for result in collected
        for claim in result.claim_results
    )
    return ToolEvidenceSuiteResult(
        case_count=case_count,
        passed_count=sum(result.passed for result in collected),
        pass_rate=_rate(sum(result.passed for result in collected), case_count, empty=0.0),
        trace_sufficiency_rate=_rate(sum(result.trace_sufficient for result in collected), case_count, empty=0.0),
        argument_constraint_pass_rate=_rate(
            sum(result.argument_constraint_pass_count for result in collected), constraint_count
        ),
        missing_required_argument_rate=_rate(
            sum(result.missing_required_argument_count for result in collected), constraint_count
        ),
        scope_overreach_count=sum(result.scope_overreach_count for result in collected),
        tool_result_availability_rate=_rate(
            sum(result.successful_result_count for result in collected), required_results
        ),
        cited_source_coverage=_rate(citation_supported, required_claims),
        unknown_citation_count=sum(len(result.unknown_citations) for result in collected),
        required_claim_support_rate=_rate(
            sum(result.supported_claim_count for result in collected), required_claims
        ),
        unsupported_claim_count=sum(len(result.unsupported_claim_ids) for result in collected),
        forbidden_claim_violation_count=sum(
            len(result.forbidden_claim_violations) for result in collected
        ),
        results=collected,
    )


__all__ = [
    "aggregate_tool_evidence_results",
    "evaluate_tool_evidence",
    "load_tool_evidence_events",
    "load_tool_evidence_spool",
    "observation_from_tool_evidence_events",
    "replay_tool_evidence_events",
    "replay_tool_evidence_run",
    "replay_tool_evidence_spool",
]
