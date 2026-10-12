"""Capability-based shadow routing for purpose-sized portfolio reads.

This module never changes the served Workspace answer. It uses one compact LLM
planner to decide whether a request fits the strict portfolio_query_v2 contract,
executes only typed reads, and emits privacy-safe comparison metadata.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable, Literal

from pydantic import ValidationError

from src.agent.project_resolve import is_conformance_case_lookup
from src.assistant.portfolio_query_v2 import portfolio_query_v2, render_portfolio_query_v2
from src.assistant.portfolio_query_v2_agent import (
    PortfolioQueryV2Plan,
    plan_portfolio_query_v2,
    repair_portfolio_query_v2_plan,
)
from src.assistant.portfolio_query_v2_semantic_planner import (
    plan_portfolio_query_v2_semantically,
)
from src.observability.contracts import TraceContext, trace_context_scope
from src.observability.lifecycle import emit_event, emit_run_finish, emit_run_start

TOOL_CENTRIC_SHADOW_ROUTER_VERSION = "workspace-tool-centric-shadow-router-v1"
TOOL_CENTRIC_SHADOW_EVENT = "assistant.tool_centric_shadow"

RouteName = Literal["portfolio_typed_read", "fallback"]
ExecutionStatus = Literal["completed", "fallback", "failed"]

_UNRESOLVED_REFERENCES = (
    "those projects",
    "these projects",
    "the above projects",
    "the previous projects",
    "same projects",
    "嗰啲專案",
    "呢啲專案",
    "上述專案",
    "剛才啲專案",
)


@dataclass(frozen=True)
class ToolCentricRouteDecision:
    """Validated routing decision with private typed-query state."""

    route: RouteName
    reason_code: str
    status: Literal["routed", "fallback", "error"]
    scope_mode: str
    project_count: int
    planner_answer_mode: str | None = None
    supported_operation: str | None = None
    plan: PortfolioQueryV2Plan | None = None
    planner_duration_ms: float = 0.0
    planner_model: str | None = None
    estimated_input_tokens: int = 0
    estimated_output_tokens: int = 0
    has_trusted_previous_query: bool = False
    error_class: str | None = None

    def safe_metadata(self) -> dict[str, Any]:
        query_hash = None
        if self.plan is not None and self.plan.query is not None:
            query_hash = _stable_hash(self.plan.query.model_dump(mode="json"))
        return {
            "router_version": TOOL_CENTRIC_SHADOW_ROUTER_VERSION,
            "route": self.route,
            "reason_code": self.reason_code,
            "decision_status": self.status,
            "planner_answer_mode": self.planner_answer_mode,
            "supported_operation": self.supported_operation,
            "scope_mode": self.scope_mode,
            "scope_project_count": self.project_count,
            "planner_duration_ms": round(self.planner_duration_ms, 3),
            "planner_model": self.planner_model,
            "estimated_input_tokens": self.estimated_input_tokens,
            "estimated_output_tokens": self.estimated_output_tokens,
            "has_trusted_previous_query": self.has_trusted_previous_query,
            "query_hash": query_hash,
            "error_class": self.error_class,
        }


@dataclass(frozen=True)
class ToolCentricShadowObservation:
    """Result of one isolated shadow execution."""

    execution_status: ExecutionStatus
    route: RouteName
    reason_code: str
    elapsed_ms: float
    planner_ms: float
    tool_ms: float = 0.0
    render_ms: float = 0.0
    llm_calls: int = 0
    estimated_input_tokens: int = 0
    estimated_output_tokens: int = 0
    candidate_answer: str | None = None
    candidate_hash: str | None = None
    served_answer_hash: str | None = None
    answer_exact_match: bool = False
    matched_project_count: int | None = None
    selected_project_signature: str | None = None
    operation: str | None = None
    error_class: str | None = None
    shadow_run_id: str | None = None
    executed_query: dict[str, Any] | None = field(default=None, repr=False, compare=False)

    def safe_metadata(self) -> dict[str, Any]:
        return {
            "router_version": TOOL_CENTRIC_SHADOW_ROUTER_VERSION,
            "execution_status": self.execution_status,
            "route": self.route,
            "reason_code": self.reason_code,
            "elapsed_ms": round(self.elapsed_ms, 3),
            "planner_ms": round(self.planner_ms, 3),
            "tool_ms": round(self.tool_ms, 3),
            "render_ms": round(self.render_ms, 3),
            "llm_calls": self.llm_calls,
            "estimated_input_tokens": self.estimated_input_tokens,
            "estimated_output_tokens": self.estimated_output_tokens,
            "candidate_hash": self.candidate_hash,
            "served_answer_hash": self.served_answer_hash,
            "answer_exact_match": self.answer_exact_match,
            "matched_project_count": self.matched_project_count,
            "selected_project_signature": self.selected_project_signature,
            "operation": self.operation,
            "error_class": self.error_class,
            "served_answer_unchanged": True,
            "production_routing_changed": False,
        }


Planner = Callable[..., tuple[PortfolioQueryV2Plan, dict[str, Any]]]
Tool = Callable[[sqlite3.Connection, dict[str, Any]], dict[str, Any]]
Renderer = Callable[..., str]


def route_tool_centric_shadow(
    question: str,
    *,
    scope_mode: str,
    project_ids: list[str],
    as_of: datetime,
    previous_query: dict[str, Any] | None = None,
    model_override: str | None = None,
    planner_timeout_seconds: int = 20,
    planner: Planner | None = None,
) -> ToolCentricRouteDecision:
    """Use the strict planner as a capability router; never execute a tool here."""
    normalized_scope = str(scope_mode or "")
    normalized_ids = list(dict.fromkeys(str(item).strip() for item in project_ids if str(item).strip()))
    if normalized_scope not in {"portfolio", "projects"}:
        return _fallback(
            "invalid_trusted_scope",
            normalized_scope,
            normalized_ids,
            status="error",
            error_class="ValueError",
            previous_query=previous_query,
        )
    if normalized_scope == "projects" and not normalized_ids:
        return _fallback(
            "empty_selected_scope",
            normalized_scope,
            normalized_ids,
            previous_query=previous_query,
        )
    if _has_unresolved_reference(question) and previous_query is None:
        return _fallback(
            "unresolved_followup_reference",
            normalized_scope,
            normalized_ids,
            previous_query=previous_query,
        )
    if is_conformance_case_lookup(question):
        return _fallback(
            "conformance_case_lookup",
            normalized_scope,
            normalized_ids,
            previous_query=previous_query,
        )

    started = time.monotonic()
    try:
        if planner is None:
            semantic = plan_portfolio_query_v2_semantically(question, as_of=as_of)
        else:
            # Injected planners remain authoritative in tests and evaluation harnesses.
            semantic = None
        if semantic is not None:
            plan = PortfolioQueryV2Plan.model_validate(semantic.plan_payload())
            metadata = semantic.metadata()
        else:
            planner_fn = planner or plan_portfolio_query_v2
            plan, metadata = planner_fn(
                question,
                scope_mode=normalized_scope,
                project_ids=normalized_ids,
                as_of=as_of,
                previous_query=previous_query,
                model_override=model_override,
                timeout_sec=max(1, int(planner_timeout_seconds)),
            )
        plan = repair_portfolio_query_v2_plan(question, plan)
        elapsed_ms = (time.monotonic() - started) * 1000
    except Exception as exc:
        elapsed_ms = (time.monotonic() - started) * 1000
        return ToolCentricRouteDecision(
            route="fallback",
            reason_code=_planner_error_reason(exc),
            status="error",
            scope_mode=normalized_scope,
            project_count=len(normalized_ids),
            planner_duration_ms=elapsed_ms,
            has_trusted_previous_query=previous_query is not None,
            error_class=type(exc).__name__,
        )

    planner_ms = float(metadata.get("elapsed_seconds") or 0.0) * 1000 or elapsed_ms
    common = {
        "scope_mode": normalized_scope,
        "project_count": len(normalized_ids),
        "planner_answer_mode": plan.answer_mode,
        "plan": plan,
        "planner_duration_ms": planner_ms,
        "planner_model": metadata.get("model"),
        "estimated_input_tokens": int(metadata.get("estimated_input_tokens") or 0),
        "estimated_output_tokens": int(metadata.get("estimated_output_tokens") or 0),
        "has_trusted_previous_query": previous_query is not None,
    }
    if plan.answer_mode == "typed" and plan.query is not None:
        return ToolCentricRouteDecision(
            route="portfolio_typed_read",
            reason_code="supported_typed_portfolio_read",
            status="routed",
            supported_operation=plan.query.operation,
            **common,
        )
    if plan.answer_mode == "synthesis":
        return ToolCentricRouteDecision(
            route="fallback",
            reason_code="requires_synthesis",
            status="fallback",
            supported_operation=plan.query.operation if plan.query else None,
            **common,
        )
    if plan.answer_mode == "clarification":
        return ToolCentricRouteDecision(
            route="fallback",
            reason_code="requires_clarification",
            status="fallback",
            **common,
        )
    return ToolCentricRouteDecision(
        route="fallback",
        reason_code="unsupported_capability",
        status="fallback",
        **common,
    )


def observe_tool_centric_shadow(
    conn: sqlite3.Connection,
    *,
    question: str,
    served_answer: str,
    scope_mode: str,
    project_ids: list[str],
    as_of: datetime | None = None,
    previous_query: dict[str, Any] | None = None,
    model_override: str | None = None,
    planner_timeout_seconds: int = 20,
    parent_trace_id: str | None = None,
    parent_run_id: str | None = None,
    thread_id: str | None = None,
    turn_id: str | None = None,
    planner: Planner | None = None,
    tool: Tool | None = None,
    renderer: Renderer | None = None,
) -> ToolCentricShadowObservation:
    """Execute one isolated candidate and always leave the served answer unchanged."""
    current = as_of or datetime.now(timezone.utc)
    if current.tzinfo is None:
        current = current.replace(tzinfo=timezone.utc)
    trace_context = TraceContext.create(
        surface="background",
        task="tool_centric_shadow",
        thread_id=thread_id,
        turn_id=turn_id,
        trace_id=parent_trace_id,
    )
    total_started = time.monotonic()
    served_hash = _stable_hash(served_answer)

    with trace_context_scope(trace_context):
        _safe_run_start(
            trace_context,
            metadata={
                "router_version": TOOL_CENTRIC_SHADOW_ROUTER_VERSION,
                "parent_run_id": parent_run_id,
                "scope_mode": scope_mode,
                "scope_project_count": len(project_ids),
                "production_routing_changed": False,
            },
            payload={"question": question, "served_answer": served_answer},
        )
        decision = route_tool_centric_shadow(
            question,
            scope_mode=scope_mode,
            project_ids=project_ids,
            as_of=current,
            previous_query=previous_query,
            model_override=model_override,
            planner_timeout_seconds=planner_timeout_seconds,
            planner=planner,
        )
        _safe_event(
            trace_context,
            "planner.decision",
            status="ok" if decision.status != "error" else "error",
            duration_ms=decision.planner_duration_ms,
            metadata=decision.safe_metadata(),
            payload={"plan": _plan_payload(decision)},
        )

        if decision.route == "fallback":
            observation = ToolCentricShadowObservation(
                execution_status="fallback",
                route=decision.route,
                reason_code=decision.reason_code,
                elapsed_ms=(time.monotonic() - total_started) * 1000,
                planner_ms=decision.planner_duration_ms,
                llm_calls=1 if decision.planner_answer_mode is not None or decision.error_class else 0,
                estimated_input_tokens=decision.estimated_input_tokens,
                estimated_output_tokens=decision.estimated_output_tokens,
                served_answer_hash=served_hash,
                error_class=decision.error_class,
                shadow_run_id=trace_context.run_id,
            )
            _emit_observation(
                trace_context,
                observation,
                parent_run_id=parent_run_id,
                payload={"question": question, "served_answer": served_answer, "plan": _plan_payload(decision)},
            )
            _safe_run_finish(
                trace_context,
                status="completed" if decision.status != "error" else "error",
                exit_reason=decision.reason_code,
                metadata={**observation.safe_metadata(), "parent_run_id": parent_run_id},
            )
            return observation

        query = decision.plan.query if decision.plan else None
        if query is None:
            observation = _failed_observation(
                decision, total_started, served_hash, "InvalidTypedPlan", trace_context.run_id
            )
            _finish_failed(trace_context, observation, parent_run_id, question, served_answer, decision)
            return observation

        sources: list[dict[str, Any]] = []
        tool_fn = tool or portfolio_query_v2
        renderer_fn = renderer or render_portfolio_query_v2
        tool_ms = 0.0
        try:
            _safe_event(
                trace_context,
                "tool.start",
                status="running",
                metadata={"tool_name": "portfolio_query_v2", "operation": query.operation},
                payload={"arguments": query.model_dump(mode="json")},
            )
            tool_started = time.monotonic()
            result = tool_fn(
                conn,
                {
                    **query.model_dump(mode="json"),
                    "_assistant_scope_mode": scope_mode,
                    "_assistant_scope_project_ids": list(project_ids),
                    "_assistant_as_of": current.isoformat(),
                    "_assistant_source_sink": sources,
                },
            )
            tool_ms = (time.monotonic() - tool_started) * 1000
            _safe_event(
                trace_context,
                "tool.end",
                status="ok",
                duration_ms=tool_ms,
                metadata={
                    "tool_name": "portfolio_query_v2",
                    "operation": result.get("operation"),
                    "matched_project_count": result.get("matched_project_count"),
                    "selected_project_signature": result.get("selected_project_signature"),
                    "result_bytes": len(json.dumps(result, ensure_ascii=False, default=str).encode("utf-8")),
                },
                payload={"result": result, "sources": sources},
            )
            render_started = time.monotonic()
            candidate = str(renderer_fn(result, language=decision.plan.language) or "").strip()
            render_ms = (time.monotonic() - render_started) * 1000
            if not candidate:
                raise ValueError("tool-centric renderer returned an empty answer")
        except Exception as exc:
            observation = _failed_observation(
                decision,
                total_started,
                served_hash,
                type(exc).__name__,
                trace_context.run_id,
                tool_ms=tool_ms,
            )
            _safe_event(
                trace_context,
                "tool.error",
                status="error",
                metadata={"tool_name": "portfolio_query_v2", "error_class": type(exc).__name__},
            )
            _finish_failed(trace_context, observation, parent_run_id, question, served_answer, decision)
            return observation

        observation = ToolCentricShadowObservation(
            execution_status="completed",
            route=decision.route,
            reason_code=decision.reason_code,
            elapsed_ms=(time.monotonic() - total_started) * 1000,
            planner_ms=decision.planner_duration_ms,
            tool_ms=tool_ms,
            render_ms=render_ms,
            llm_calls=1,
            estimated_input_tokens=decision.estimated_input_tokens,
            estimated_output_tokens=decision.estimated_output_tokens,
            candidate_answer=candidate,
            candidate_hash=_stable_hash(candidate),
            served_answer_hash=served_hash,
            answer_exact_match=candidate == served_answer,
            matched_project_count=int(result.get("matched_project_count") or 0),
            selected_project_signature=str(result.get("selected_project_signature") or "") or None,
            operation=str(result.get("operation") or "") or None,
            shadow_run_id=trace_context.run_id,
            executed_query=query.model_dump(mode="json"),
        )
        _emit_observation(
            trace_context,
            observation,
            parent_run_id=parent_run_id,
            payload={
                "question": question,
                "served_answer": served_answer,
                "candidate_answer": candidate,
                "plan": _plan_payload(decision),
                "tool_result": result,
                "sources": sources,
            },
        )
        _safe_run_finish(
            trace_context,
            status="completed",
            exit_reason="shadow_typed_result",
            final_output=candidate,
            metadata={**observation.safe_metadata(), "parent_run_id": parent_run_id},
        )
        return observation


def _fallback(
    reason_code: str,
    scope_mode: str,
    project_ids: list[str],
    *,
    status: Literal["fallback", "error"] = "fallback",
    error_class: str | None = None,
    previous_query: dict[str, Any] | None = None,
) -> ToolCentricRouteDecision:
    return ToolCentricRouteDecision(
        route="fallback",
        reason_code=reason_code,
        status=status,
        scope_mode=scope_mode,
        project_count=len(project_ids),
        has_trusted_previous_query=previous_query is not None,
        error_class=error_class,
    )


def _has_unresolved_reference(question: str) -> bool:
    text = " ".join(str(question or "").casefold().split())
    return any(phrase.casefold() in text for phrase in _UNRESOLVED_REFERENCES)


def _planner_error_reason(exc: Exception) -> str:
    name = type(exc).__name__.casefold()
    message = str(exc).casefold()
    if "timeout" in name or "timed out" in message:
        return "planner_timeout"
    if isinstance(exc, (ValidationError, ValueError, TypeError, json.JSONDecodeError)):
        return "invalid_plan"
    return "planner_error"


def _failed_observation(
    decision: ToolCentricRouteDecision,
    started: float,
    served_hash: str,
    error_class: str,
    shadow_run_id: str,
    *,
    tool_ms: float = 0.0,
) -> ToolCentricShadowObservation:
    return ToolCentricShadowObservation(
        execution_status="failed",
        route=decision.route,
        reason_code="candidate_execution_failed",
        elapsed_ms=(time.monotonic() - started) * 1000,
        planner_ms=decision.planner_duration_ms,
        tool_ms=tool_ms,
        llm_calls=1,
        estimated_input_tokens=decision.estimated_input_tokens,
        estimated_output_tokens=decision.estimated_output_tokens,
        served_answer_hash=served_hash,
        operation=decision.supported_operation,
        error_class=error_class,
        shadow_run_id=shadow_run_id,
    )


def _finish_failed(
    context: TraceContext,
    observation: ToolCentricShadowObservation,
    parent_run_id: str | None,
    question: str,
    served_answer: str,
    decision: ToolCentricRouteDecision,
) -> None:
    _emit_observation(
        context,
        observation,
        parent_run_id=parent_run_id,
        payload={
            "question": question,
            "served_answer": served_answer,
            "plan": _plan_payload(decision),
        },
    )
    _safe_run_finish(
        context,
        status="error",
        exit_reason=observation.reason_code,
        anomaly_flags=[observation.error_class] if observation.error_class else [],
        metadata={**observation.safe_metadata(), "parent_run_id": parent_run_id},
    )


def _emit_observation(
    context: TraceContext,
    observation: ToolCentricShadowObservation,
    *,
    parent_run_id: str | None,
    payload: dict[str, Any],
) -> None:
    _safe_event(
        context,
        TOOL_CENTRIC_SHADOW_EVENT,
        status={"completed": "ok", "fallback": "skipped", "failed": "error"}[observation.execution_status],
        duration_ms=observation.elapsed_ms,
        metadata={**observation.safe_metadata(), "parent_run_id": parent_run_id},
        payload=payload,
    )


def _plan_payload(decision: ToolCentricRouteDecision) -> dict[str, Any] | None:
    return decision.plan.model_dump(mode="json") if decision.plan else None


def _safe_event(context: TraceContext, event_type: str, **kwargs: Any) -> None:
    try:
        emit_event(context, event_type, **kwargs)
    except Exception:
        pass


def _safe_run_start(context: TraceContext, **kwargs: Any) -> None:
    try:
        emit_run_start(context, **kwargs)
    except Exception:
        pass


def _safe_run_finish(context: TraceContext, **kwargs: Any) -> None:
    try:
        emit_run_finish(context, **kwargs)
    except Exception:
        pass


def _stable_hash(value: Any) -> str:
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, default=str).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


__all__ = [
    "TOOL_CENTRIC_SHADOW_EVENT",
    "TOOL_CENTRIC_SHADOW_ROUTER_VERSION",
    "ToolCentricRouteDecision",
    "ToolCentricShadowObservation",
    "observe_tool_centric_shadow",
    "route_tool_centric_shadow",
]
