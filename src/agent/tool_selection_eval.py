"""Deterministic semantic evaluation and replay for agent tool selection traces."""

from __future__ import annotations

import json
import sqlite3
from collections import Counter
from collections.abc import Iterable, Mapping, Sequence
from typing import Any

from src.agent.tool_selection_eval_models import (
    FUNNEL_KEYS,
    ToolSelectionCaseResult,
    ToolSelectionEvalCase,
    ToolSelectionFunnel,
    ToolSelectionObservation,
    ToolSelectionRound,
    ToolSelectionSuiteResult,
    ToolSelectionTraceIssue,
)

def _rate(numerator: int | float, denominator: int | float, *, empty: float = 0.0) -> float:
    if not denominator:
        return empty
    return round(float(numerator) / float(denominator), 6)


def _ordered_unique(values: Iterable[str]) -> tuple[str, ...]:
    return tuple(dict.fromkeys(value for value in values if value))


def _coerce_mapping(raw: Any) -> dict[str, Any]:
    if hasattr(raw, "model_dump"):
        raw = raw.model_dump()
    if isinstance(raw, sqlite3.Row):
        raw = dict(raw)
    if not isinstance(raw, Mapping):
        raise TypeError(f"trace event must be a mapping, got {type(raw).__name__}")
    event = raw.get("event")
    if "event_type" not in raw and isinstance(event, Mapping):
        return dict(event)
    return dict(raw)


def _metadata(
    event: Mapping[str, Any],
    issues: list[ToolSelectionTraceIssue],
) -> dict[str, Any]:
    value = event.get("metadata")
    if value is None:
        value = event.get("metadata_json")
    if value is None:
        return {}
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except (TypeError, ValueError):
            issues.append(ToolSelectionTraceIssue(
                code="invalid_metadata_json",
                severity="error",
                message="Trace event metadata_json is not valid JSON.",
                event_id=str(event.get("event_id") or "") or None,
            ))
            return {}
    if not isinstance(value, Mapping):
        issues.append(ToolSelectionTraceIssue(
            code="invalid_metadata_shape",
            severity="error",
            message="Trace event metadata must be an object.",
            event_id=str(event.get("event_id") or "") or None,
        ))
        return {}
    return dict(value)


def _tool_names(value: Any) -> tuple[str, ...]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
        return ()
    return tuple(str(name).strip() for name in value if str(name).strip())


def _funnel_from_metadata(
    metadata: Mapping[str, Any],
    *,
    event_id: str | None,
    issues: list[ToolSelectionTraceIssue],
) -> ToolSelectionFunnel:
    raw = metadata.get("tool_selection_funnel")
    if not isinstance(raw, Mapping):
        issues.append(ToolSelectionTraceIssue(
            code="missing_tool_selection_funnel",
            severity="error",
            message="round.end is missing tool_selection_funnel metadata.",
            event_id=event_id,
        ))
        return ToolSelectionFunnel()
    missing_keys = [key for key in FUNNEL_KEYS if key not in raw]
    if missing_keys:
        issues.append(ToolSelectionTraceIssue(
            code="incomplete_tool_selection_funnel",
            severity="error",
            message=f"tool_selection_funnel is missing keys: {missing_keys}",
            event_id=event_id,
        ))
    values: dict[str, int] = {}
    for key in FUNNEL_KEYS:
        try:
            value = int(raw.get(key, 0))
            if value < 0:
                raise ValueError
            values[key] = value
        except (TypeError, ValueError):
            issues.append(ToolSelectionTraceIssue(
                code="invalid_tool_selection_funnel",
                severity="error",
                message=f"tool_selection_funnel.{key} must be a non-negative integer.",
                event_id=event_id,
            ))
            values[key] = 0
    return ToolSelectionFunnel(**values)


def observation_from_trace_events(events: Iterable[Any]) -> ToolSelectionObservation:
    """Reconstruct tool-selection rounds from in-memory, spool, or SQLite events."""

    issues: list[ToolSelectionTraceIssue] = []
    normalized: list[tuple[int, int, dict[str, Any], dict[str, Any]]] = []
    for position, raw in enumerate(events):
        try:
            event = _coerce_mapping(raw)
        except TypeError as exc:
            issues.append(ToolSelectionTraceIssue(
                code="invalid_trace_event",
                severity="error",
                message=str(exc),
            ))
            continue
        metadata = _metadata(event, issues)
        try:
            sequence = int(event.get("sequence"))
        except (TypeError, ValueError):
            sequence = 1_000_000_000 + position
        normalized.append((sequence, position, event, metadata))
    normalized.sort(key=lambda item: (item[0], item[1]))

    run_ids = _ordered_unique(str(item[2].get("run_id") or "") for item in normalized)
    trace_ids = _ordered_unique(str(item[2].get("trace_id") or "") for item in normalized)
    if len(run_ids) > 1:
        issues.append(ToolSelectionTraceIssue(
            code="multiple_run_ids",
            severity="error",
            message=f"Replay input contains multiple run IDs: {list(run_ids)}",
        ))
    if len(trace_ids) > 1:
        issues.append(ToolSelectionTraceIssue(
            code="multiple_trace_ids",
            severity="error",
            message=f"Replay input contains multiple trace IDs: {list(trace_ids)}",
        ))

    surface = role = intent = None
    prepared_tools: tuple[str, ...] = ()
    round_starts: dict[int, tuple[str, ...]] = {}
    rounds: list[ToolSelectionRound] = []
    fallback_round = 0

    for sequence, _, event, metadata in normalized:
        event_type = str(event.get("event_type") or "")
        if event_type == "run.start":
            surface = str(metadata.get("surface") or "") or surface
            role = str(metadata.get("role") or "") or role
            intent = str(metadata.get("intent") or metadata.get("task") or "") or intent
            prepared_tools = _tool_names(metadata.get("tool_names")) or prepared_tools
        elif event_type in {"context.composed", "prepare.snapshot"}:
            surface = str(metadata.get("surface") or "") or surface
            role = str(metadata.get("role") or "") or role
            intent = str(metadata.get("intent") or "") or intent
            prepared_tools = _tool_names(metadata.get("tool_names")) or prepared_tools
        elif event_type == "round.start":
            try:
                round_index = int(event.get("round_index"))
            except (TypeError, ValueError):
                issues.append(ToolSelectionTraceIssue(
                    code="missing_round_index",
                    severity="error",
                    message="round.start is missing a valid round_index.",
                    event_id=str(event.get("event_id") or "") or None,
                ))
                continue
            round_starts[round_index] = _tool_names(metadata.get("active_tool_names"))
        elif event_type == "round.end":
            fallback_round += 1
            try:
                round_index = int(event.get("round_index"))
                if round_index < 1:
                    raise ValueError
            except (TypeError, ValueError):
                round_index = fallback_round
                issues.append(ToolSelectionTraceIssue(
                    code="missing_round_index",
                    severity="error",
                    message=f"round.end used fallback round index {round_index}.",
                    event_id=str(event.get("event_id") or "") or None,
                ))
            event_id = str(event.get("event_id") or "") or None
            selected_tools = _tool_names(metadata.get("tool_names"))
            funnel = _funnel_from_metadata(
                metadata,
                event_id=event_id,
                issues=issues,
            )
            if funnel.selected != len(selected_tools):
                issues.append(ToolSelectionTraceIssue(
                    code="selected_count_mismatch",
                    severity="error",
                    message=(
                        f"round {round_index} reports {funnel.selected} selections "
                        f"but names {len(selected_tools)} tools."
                    ),
                    event_id=event_id,
                ))
            if funnel.authorized > funnel.selected or funnel.executed > funnel.authorized:
                issues.append(ToolSelectionTraceIssue(
                    code="invalid_funnel_order",
                    severity="error",
                    message=(
                        f"round {round_index} violates selected >= authorized >= executed."
                    ),
                    event_id=event_id,
                ))
            if funnel.blocked != max(0, funnel.selected - funnel.executed):
                issues.append(ToolSelectionTraceIssue(
                    code="blocked_count_mismatch",
                    severity="error",
                    message=(
                        f"round {round_index} blocked count does not equal selected - executed."
                    ),
                    event_id=event_id,
                ))
            rounds.append(ToolSelectionRound(
                round_index=round_index,
                event_id=event_id,
                sequence=sequence if sequence < 1_000_000_000 else None,
                active_tools=round_starts.get(round_index, prepared_tools),
                selected_tools=selected_tools,
                observable_outcome=(
                    str(metadata.get("observable_outcome"))
                    if metadata.get("observable_outcome") is not None
                    else None
                ),
                funnel=funnel,
            ))

    if not rounds:
        issues.append(ToolSelectionTraceIssue(
            code="no_round_end_events",
            severity="error",
            message="Replay input contains no round.end events.",
        ))
    observed_available = _ordered_unique(
        tool
        for round_observation in rounds
        for tool in round_observation.active_tools
    ) or prepared_tools
    return ToolSelectionObservation(
        run_id=run_ids[0] if run_ids else None,
        trace_id=trace_ids[0] if trace_ids else None,
        surface=surface,
        role=role,
        intent=intent,
        available_tools=observed_available,
        rounds=tuple(rounds),
        issues=tuple(issues),
    )


def evaluate_tool_selection(
    case: ToolSelectionEvalCase,
    observation: ToolSelectionObservation,
) -> ToolSelectionCaseResult:
    selected_tools = tuple(
        tool for round_observation in observation.rounds for tool in round_observation.selected_tools
    )
    selected_counts = Counter(selected_tools)
    selected_set = set(selected_tools)
    expected = set(case.expected_tools)
    forbidden = set(case.forbidden_tools)
    observed_available = set(observation.available_tools)

    true_positive = tuple(tool for tool in case.expected_tools if tool in selected_set)
    missing = tuple(tool for tool in case.expected_tools if tool not in selected_set)
    unexpected = _ordered_unique(tool for tool in selected_tools if tool not in expected)
    forbidden_selected = tuple(tool for tool in case.forbidden_tools if tool in selected_set)
    unexposed = tuple(tool for tool in case.expected_tools if tool not in observed_available)
    drift = tuple(sorted(set(case.available_tools) ^ observed_available)) if observed_available else ()

    funnels = [round_observation.funnel for round_observation in observation.rounds]
    selected_count = sum(funnel.selected for funnel in funnels)
    authorized_count = sum(funnel.authorized for funnel in funnels)
    executed_count = sum(funnel.executed for funnel in funnels)
    authorization_denials = sum(
        max(0, funnel.selected - funnel.authorized) for funnel in funnels
    )
    pre_execution_blocks = sum(
        max(0, funnel.authorized - funnel.executed) for funnel in funnels
    )
    duplicate_count = sum(funnel.duplicate_selection for funnel in funnels)
    unnecessary_count = sum(
        count for tool, count in selected_counts.items() if tool not in expected
    )
    forbidden_count = sum(
        count for tool, count in selected_counts.items() if tool in forbidden
    )

    precision = _rate(len(true_positive), len(set(selected_tools)), empty=1.0 if not expected else 0.0)
    recall = _rate(len(true_positive), len(expected), empty=1.0)
    f1 = _rate(2 * precision * recall, precision + recall)
    exact_match = selected_set == expected
    trace_valid = not any(issue.severity == "error" for issue in observation.issues)
    context_match = all((
        observation.surface in (None, case.surface),
        observation.role in (None, case.role),
        observation.intent in (None, case.intent),
    ))

    findings: list[str] = []
    if missing:
        findings.append("required_tool_miss")
    if unexpected:
        findings.append("unnecessary_tool_selection")
    if forbidden_selected:
        findings.append("forbidden_tool_selection")
    if duplicate_count:
        findings.append("duplicate_tool_selection")
    if authorization_denials:
        findings.append("authorization_block")
    if pre_execution_blocks:
        findings.append("pre_execution_block")
    if unexposed:
        findings.append("required_tool_not_exposed")
    if drift:
        findings.append("available_tool_contract_drift")
    if not context_match:
        findings.append("trace_context_mismatch")
    if not trace_valid:
        findings.append("invalid_trace_contract")

    passed = all((
        exact_match,
        not forbidden_selected,
        duplicate_count == 0,
        authorization_denials == 0,
        pre_execution_blocks == 0,
        trace_valid,
        context_match,
    ))
    return ToolSelectionCaseResult(
        case_id=case.case_id,
        run_id=observation.run_id,
        selected_tools=selected_tools,
        selected_tool_counts=dict(sorted(selected_counts.items())),
        true_positive_tools=true_positive,
        missing_required_tools=missing,
        unexpected_tools=unexpected,
        forbidden_tools_selected=forbidden_selected,
        unexposed_required_tools=unexposed,
        available_tool_drift=drift,
        selected_count=selected_count,
        authorized_count=authorized_count,
        authorization_denial_count=authorization_denials,
        pre_execution_block_count=pre_execution_blocks,
        executed_count=executed_count,
        duplicate_selection_count=duplicate_count,
        unnecessary_selection_count=unnecessary_count,
        forbidden_selection_count=forbidden_count,
        precision=precision,
        recall=recall,
        f1=f1,
        authorization_block_rate=_rate(authorization_denials, selected_count),
        exact_match=exact_match,
        trace_valid=trace_valid,
        context_match=context_match,
        passed=passed,
        findings=tuple(findings),
        observation=observation,
    )


def replay_tool_selection_events(
    case: ToolSelectionEvalCase,
    events: Iterable[Any],
) -> ToolSelectionCaseResult:
    """Evaluate a stored event stream without invoking a live model or tool."""

    return evaluate_tool_selection(case, observation_from_trace_events(events))


def load_tool_selection_events(
    conn: sqlite3.Connection,
    run_id: str,
) -> list[dict[str, Any]]:
    cursor = conn.execute(
        """SELECT event_id,run_id,trace_id,sequence,event_type,round_index,metadata_json
           FROM trace_events WHERE run_id=? ORDER BY sequence""",
        (run_id,),
    )
    columns = [description[0] for description in cursor.description or ()]
    rows = [dict(zip(columns, row)) for row in cursor.fetchall()]
    if not rows:
        raise KeyError(run_id)
    return rows


def replay_tool_selection_run(
    conn: sqlite3.Connection,
    case: ToolSelectionEvalCase,
    run_id: str,
) -> ToolSelectionCaseResult:
    """Load and replay one persisted engineering trace run."""

    return replay_tool_selection_events(case, load_tool_selection_events(conn, run_id))


def aggregate_tool_selection_results(
    results: Iterable[ToolSelectionCaseResult],
) -> ToolSelectionSuiteResult:
    collected = tuple(results)
    case_count = len(collected)
    selected_count = sum(result.selected_count for result in collected)
    expected_count = sum(
        len(result.true_positive_tools) + len(result.missing_required_tools)
        for result in collected
    )
    return ToolSelectionSuiteResult(
        case_count=case_count,
        passed_count=sum(result.passed for result in collected),
        pass_rate=_rate(sum(result.passed for result in collected), case_count),
        exact_match_rate=_rate(sum(result.exact_match for result in collected), case_count),
        macro_precision=_rate(sum(result.precision for result in collected), case_count),
        macro_recall=_rate(sum(result.recall for result in collected), case_count),
        macro_f1=_rate(sum(result.f1 for result in collected), case_count),
        required_tool_miss_rate=_rate(
            sum(len(result.missing_required_tools) for result in collected),
            expected_count,
        ),
        unnecessary_selection_rate=_rate(
            sum(result.unnecessary_selection_count for result in collected),
            selected_count,
        ),
        forbidden_selection_rate=_rate(
            sum(result.forbidden_selection_count for result in collected),
            selected_count,
        ),
        duplicate_selection_rate=_rate(
            sum(result.duplicate_selection_count for result in collected),
            selected_count,
        ),
        authorization_block_rate=_rate(
            sum(result.authorization_denial_count for result in collected),
            selected_count,
        ),
        results=collected,
    )


__all__ = [
    "ToolSelectionCaseResult",
    "ToolSelectionEvalCase",
    "ToolSelectionFunnel",
    "ToolSelectionObservation",
    "ToolSelectionRound",
    "ToolSelectionSuiteResult",
    "ToolSelectionTraceIssue",
    "aggregate_tool_selection_results",
    "evaluate_tool_selection",
    "load_tool_selection_events",
    "observation_from_trace_events",
    "replay_tool_selection_events",
    "replay_tool_selection_run",
]
