"""Shared fail-open serving path for purpose-sized portfolio reads.

Workspace and Telegram both call this module before composing broad Agent
context. Only validated typed reads are served directly. Every unsupported,
ambiguous, slow, invalid, or failed attempt returns a fallback result so the
existing Agent loop remains authoritative for all other requests.
"""

from __future__ import annotations

import json
import sqlite3
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable, Literal

from src.assistant.portfolio_query_v2 import portfolio_query_v2
from src.assistant.portfolio_query_v2_renderer import render_portfolio_query_v2
from src.assistant.tool_centric_query_memory import (
    get_previous_tool_centric_query,
    remember_tool_centric_query,
)
from src.assistant.tool_centric_shadow_router import (
    Planner,
    Renderer,
    Tool,
    ToolCentricRouteDecision,
    route_tool_centric_shadow,
)
from src.observability.contracts import TraceContext
from src.observability.lifecycle import emit_event

TOOL_CENTRIC_SERVING_VERSION = "tool-centric-unified-serving-v1"
_SUPPORTED_OPERATIONS = frozenset({"count", "list", "exists", "group"})


@dataclass(frozen=True)
class ToolCentricServingResult:
    """Observable result of one shared serving attempt."""

    outcome: Literal["served", "fallback"]
    reason_code: str
    answer: str | None = None
    query: dict[str, Any] | None = field(default=None, repr=False, compare=False)
    tool_result: dict[str, Any] | None = field(default=None, repr=False, compare=False)
    sources: tuple[dict[str, Any], ...] = field(default=(), repr=False, compare=False)
    scope_mode: str = "portfolio"
    project_ids: tuple[str, ...] = ()
    operation: str | None = None
    planner_ms: float = 0.0
    tool_ms: float = 0.0
    render_ms: float = 0.0
    elapsed_ms: float = 0.0
    estimated_input_tokens: int = 0
    estimated_output_tokens: int = 0
    planner_model: str | None = None
    error_class: str | None = None
    renderer_language: str | None = None

    @property
    def served(self) -> bool:
        return self.outcome == "served"

    def safe_metadata(self) -> dict[str, Any]:
        return {
            "serving_version": TOOL_CENTRIC_SERVING_VERSION,
            "outcome": self.outcome,
            "reason_code": self.reason_code,
            "scope_mode": self.scope_mode,
            "scope_project_count": len(self.project_ids),
            "operation": self.operation,
            "planner_ms": round(self.planner_ms, 3),
            "tool_ms": round(self.tool_ms, 3),
            "render_ms": round(self.render_ms, 3),
            "elapsed_ms": round(self.elapsed_ms, 3),
            "estimated_input_tokens": self.estimated_input_tokens,
            "estimated_output_tokens": self.estimated_output_tokens,
            "planner_model": self.planner_model,
            "error_class": self.error_class,
            "renderer_language": self.renderer_language,
            "broad_loop_bypassed": self.served,
        }


ServingRouter = Callable[..., ToolCentricRouteDecision]


def _resolve_renderer_language(
    response_language: str | None,
    *,
    planner_language: str,
) -> Literal["en", "zh-Hant"]:
    """Choose deterministic prose language; operator preference is authoritative."""
    if response_language is None:
        return "zh-Hant" if planner_language == "zh-Hant" else "en"
    normalized = str(response_language).strip().lower()
    if normalized == "en":
        return "en"
    if normalized in {"zh", "zh-tw", "zh-cn", "zh-hant", "zh-hans"}:
        # The current deterministic renderer has one Chinese pack (Traditional).
        return "zh-Hant"
    return "zh-Hant"


def tool_centric_mode() -> str:
    """Return the effective neutral rollout mode with legacy compatibility."""
    from config import defaults

    mode = str(getattr(defaults, "TOOL_CENTRIC_MODE", "off") or "off").strip().lower()
    if mode not in {"off", "shadow", "serve"}:
        mode = "off"
    if mode == "off" and bool(
        getattr(defaults, "AI_WORKSPACE_TOOL_CENTRIC_SHADOW_ENABLED", False)
    ):
        return "shadow"
    return mode


def tool_centric_surface_enabled(surface: str) -> bool:
    """Return whether the shared rollout includes a surface."""
    from config import defaults

    enabled = getattr(
        defaults,
        "TOOL_CENTRIC_ENABLED_SURFACES",
        frozenset({"workspace", "telegram"}),
    )
    return str(surface).strip().lower() in {str(item).strip().lower() for item in enabled}


def tool_centric_planner_model() -> str:
    """Return the dedicated compact planner route, independent of the global Agent model."""
    from config import defaults

    configured = str(
        getattr(defaults, "TOOL_CENTRIC_PLANNER_MODEL", "openai/default-model")
        or ""
    ).strip()
    return configured or "openai/default-model"


def tool_centric_planner_timeout_seconds() -> int:
    """Return the bounded planner timeout used on the request path."""
    from config import defaults

    return max(1, int(getattr(defaults, "TOOL_CENTRIC_PLANNER_TIMEOUT_SECONDS", 12)))


def should_attempt_tool_centric_serving(surface: str) -> bool:
    return tool_centric_mode() == "serve" and tool_centric_surface_enabled(surface)


def should_run_tool_centric_shadow(surface: str = "workspace") -> bool:
    return tool_centric_mode() == "shadow" and tool_centric_surface_enabled(surface)


def try_tool_centric_read(
    conn: sqlite3.Connection,
    *,
    question: str,
    surface: str,
    scope_mode: str,
    project_ids: list[str] | tuple[str, ...],
    as_of: datetime | None,
    trace_context: TraceContext,
    response_language: str | None = None,
    previous_query: dict[str, Any] | None = None,
    planner_timeout_seconds: int | None = None,
    model_override: str | None = None,
    router: ServingRouter | None = None,
    planner: Planner | None = None,
    tool: Tool | None = None,
    renderer: Renderer | None = None,
) -> ToolCentricServingResult:
    """Attempt one strict typed read and otherwise fail open to the broad Agent."""
    total_started = time.monotonic()
    current = _normalize_as_of(as_of)
    normalized_ids = tuple(
        dict.fromkeys(str(item).strip() for item in project_ids if str(item).strip())
    )
    conversation_id = trace_context.thread_id
    trusted_previous_query = previous_query
    if trusted_previous_query is None:
        trusted_previous_query = get_previous_tool_centric_query(
            surface=surface,
            conversation_id=conversation_id,
            scope_mode=scope_mode,
            project_ids=normalized_ids,
        )
    route_fn = router or route_tool_centric_shadow
    timeout = planner_timeout_seconds or tool_centric_planner_timeout_seconds()
    planner_model = str(model_override or "").strip() or tool_centric_planner_model()

    try:
        decision = route_fn(
            question,
            scope_mode=scope_mode,
            project_ids=list(normalized_ids),
            as_of=current,
            previous_query=trusted_previous_query,
            model_override=planner_model,
            planner_timeout_seconds=timeout,
            planner=planner,
        )
    except Exception as exc:
        return _fallback_result(
            reason_code="router_error",
            scope_mode=scope_mode,
            project_ids=normalized_ids,
            started=total_started,
            error_class=type(exc).__name__,
            planner_model=planner_model,
            trace_context=trace_context,
            surface=surface,
        )

    _safe_event(
        trace_context,
        "planner.decision",
        status="ok" if decision.status != "error" else "error",
        duration_ms=decision.planner_duration_ms,
        metadata={
            **decision.safe_metadata(),
            "serving_version": TOOL_CENTRIC_SERVING_VERSION,
            "surface": surface,
            "serving_attempt": True,
        },
        payload={"plan": _plan_payload(decision)},
    )

    if decision.route != "portfolio_typed_read":
        return _fallback_from_decision(
            decision,
            scope_mode=scope_mode,
            project_ids=normalized_ids,
            started=total_started,
            trace_context=trace_context,
            surface=surface,
        )

    query = decision.plan.query if decision.plan is not None else None
    if query is None or query.operation not in _SUPPORTED_OPERATIONS:
        return _fallback_from_decision(
            decision,
            reason_code="invalid_typed_plan",
            scope_mode=scope_mode,
            project_ids=normalized_ids,
            started=total_started,
            trace_context=trace_context,
            surface=surface,
            error_class="InvalidTypedPlan",
        )

    if (
        decision.scope_mode != scope_mode
        or decision.project_count != len(normalized_ids)
    ):
        _safe_event(
            trace_context,
            "validation.error",
            status="error",
            metadata={
                "validation_stage": "planner_scope",
                "operation": query.operation,
                "error_class": "TrustedScopeMismatch",
                "surface": surface,
                "serving_version": TOOL_CENTRIC_SERVING_VERSION,
            },
        )
        return _fallback_from_decision(
            decision,
            reason_code="planner_scope_mismatch",
            scope_mode=scope_mode,
            project_ids=normalized_ids,
            started=total_started,
            trace_context=trace_context,
            surface=surface,
            error_class="TrustedScopeMismatch",
        )

    sources: list[dict[str, Any]] = []
    tool_fn = tool or portfolio_query_v2
    renderer_fn = renderer or render_portfolio_query_v2
    query_payload = query.model_dump(mode="json")
    tool_ms = 0.0
    render_ms = 0.0

    _safe_event(
        trace_context,
        "tool.start",
        status="running",
        metadata={
            "tool_name": "portfolio_query_v2",
            "operation": query.operation,
            "surface": surface,
            "serving_version": TOOL_CENTRIC_SERVING_VERSION,
        },
        payload={"arguments": query_payload},
    )

    tool_started = time.monotonic()
    try:
        result = tool_fn(
            conn,
            {
                **query_payload,
                "_assistant_scope_mode": scope_mode,
                "_assistant_scope_project_ids": list(normalized_ids),
                "_assistant_as_of": current.isoformat(),
                "_assistant_source_sink": sources,
            },
        )
    except Exception as exc:
        tool_ms = (time.monotonic() - tool_started) * 1000
        _safe_event(
            trace_context,
            "tool.error",
            status="error",
            duration_ms=tool_ms,
            metadata={
                "tool_name": "portfolio_query_v2",
                "operation": query.operation,
                "error_class": type(exc).__name__,
                "surface": surface,
                "serving_version": TOOL_CENTRIC_SERVING_VERSION,
            },
        )
        return _fallback_from_decision(
            decision,
            reason_code="typed_tool_failed",
            scope_mode=scope_mode,
            project_ids=normalized_ids,
            started=total_started,
            trace_context=trace_context,
            surface=surface,
            tool_ms=tool_ms,
            error_class=type(exc).__name__,
        )

    tool_ms = (time.monotonic() - tool_started) * 1000
    _safe_event(
        trace_context,
        "tool.end",
        status="ok",
        duration_ms=tool_ms,
        metadata={
            "tool_name": "portfolio_query_v2",
            "operation": result.get("operation") if isinstance(result, dict) else None,
            "matched_project_count": (
                result.get("matched_project_count") if isinstance(result, dict) else None
            ),
            "selected_project_signature": (
                result.get("selected_project_signature") if isinstance(result, dict) else None
            ),
            "result_bytes": len(
                json.dumps(result, ensure_ascii=False, default=str).encode("utf-8")
            ),
            "surface": surface,
            "serving_version": TOOL_CENTRIC_SERVING_VERSION,
        },
        payload={"result": result, "sources": sources},
    )

    validation_error = _validate_tool_result(
        result,
        trusted_scope_mode=scope_mode,
        trusted_project_ids=normalized_ids,
        operation=query.operation,
        sources=sources,
    )
    if validation_error:
        _safe_event(
            trace_context,
            "validation.error",
            status="error",
            metadata={
                "validation_stage": "typed_result",
                "operation": query.operation,
                "error_class": "TypedResultValidationError",
                "surface": surface,
                "serving_version": TOOL_CENTRIC_SERVING_VERSION,
            },
            payload={"validation_error": validation_error},
        )
        return _fallback_from_decision(
            decision,
            reason_code="typed_result_invalid",
            scope_mode=scope_mode,
            project_ids=normalized_ids,
            started=total_started,
            trace_context=trace_context,
            surface=surface,
            tool_ms=tool_ms,
            error_class="TypedResultValidationError",
        )

    render_language = _resolve_renderer_language(
        response_language,
        planner_language=decision.plan.language,
    )
    render_started = time.monotonic()
    try:
        if renderer is None:
            presentation = "telegram" if str(surface).strip().lower() == "telegram" else "workspace"
            rendered = renderer_fn(
                result,
                language=render_language,
                presentation=presentation,
            )
        else:
            # Keep injected renderers compatible with the established contract.
            rendered = renderer_fn(result, language=render_language)
        answer = str(rendered or "").strip()
    except Exception as exc:
        render_ms = (time.monotonic() - render_started) * 1000
        _safe_event(
            trace_context,
            "renderer.error",
            status="error",
            duration_ms=render_ms,
            metadata={
                "renderer": "portfolio_query_v2",
                "operation": query.operation,
                "error_class": type(exc).__name__,
                "surface": surface,
                "serving_version": TOOL_CENTRIC_SERVING_VERSION,
                "renderer_language": render_language,
                "planner_language": decision.plan.language,
                "response_language_override": response_language,
            },
        )
        return _fallback_from_decision(
            decision,
            reason_code="renderer_failed",
            scope_mode=scope_mode,
            project_ids=normalized_ids,
            started=total_started,
            trace_context=trace_context,
            surface=surface,
            tool_ms=tool_ms,
            render_ms=render_ms,
            error_class=type(exc).__name__,
        )

    render_ms = (time.monotonic() - render_started) * 1000
    if not answer:
        _safe_event(
            trace_context,
            "renderer.error",
            status="error",
            duration_ms=render_ms,
            metadata={
                "renderer": "portfolio_query_v2",
                "operation": query.operation,
                "error_class": "EmptyRenderedAnswer",
                "surface": surface,
                "serving_version": TOOL_CENTRIC_SERVING_VERSION,
            },
        )
        return _fallback_from_decision(
            decision,
            reason_code="renderer_empty_answer",
            scope_mode=scope_mode,
            project_ids=normalized_ids,
            started=total_started,
            trace_context=trace_context,
            surface=surface,
            tool_ms=tool_ms,
            render_ms=render_ms,
            error_class="EmptyRenderedAnswer",
        )

    _safe_event(
        trace_context,
        "renderer.end",
        status="ok",
        duration_ms=render_ms,
        metadata={
            "renderer": "portfolio_query_v2",
            "operation": query.operation,
            "answer_chars": len(answer),
            "surface": surface,
            "serving_version": TOOL_CENTRIC_SERVING_VERSION,
            "renderer_language": render_language,
            "planner_language": decision.plan.language,
            "response_language_override": response_language,
        },
        payload={"answer": answer},
    )
    remember_tool_centric_query(
        surface=surface,
        conversation_id=conversation_id,
        scope_mode=scope_mode,
        project_ids=normalized_ids,
        query=query_payload,
    )
    served = ToolCentricServingResult(
        outcome="served",
        reason_code=decision.reason_code,
        answer=answer,
        query=query_payload,
        tool_result=result,
        sources=tuple(dict(item) for item in sources),
        scope_mode=scope_mode,
        project_ids=normalized_ids,
        operation=query.operation,
        planner_ms=decision.planner_duration_ms,
        tool_ms=tool_ms,
        render_ms=render_ms,
        elapsed_ms=(time.monotonic() - total_started) * 1000,
        estimated_input_tokens=decision.estimated_input_tokens,
        estimated_output_tokens=decision.estimated_output_tokens,
        planner_model=decision.planner_model,
        renderer_language=render_language,
    )
    _safe_event(
        trace_context,
        "assistant.tool_centric_serving",
        status="ok",
        duration_ms=served.elapsed_ms,
        metadata={**served.safe_metadata(), "surface": surface},
        payload={
            "question": question,
            "answer": answer,
            "query": query_payload,
            "tool_result": result,
            "sources": sources,
        },
    )
    return served


def _fallback_from_decision(
    decision: ToolCentricRouteDecision,
    *,
    scope_mode: str,
    project_ids: tuple[str, ...],
    started: float,
    trace_context: TraceContext,
    surface: str,
    reason_code: str | None = None,
    tool_ms: float = 0.0,
    render_ms: float = 0.0,
    error_class: str | None = None,
) -> ToolCentricServingResult:
    return _fallback_result(
        reason_code=reason_code or decision.reason_code,
        scope_mode=scope_mode,
        project_ids=project_ids,
        started=started,
        trace_context=trace_context,
        surface=surface,
        operation=decision.supported_operation,
        planner_ms=decision.planner_duration_ms,
        tool_ms=tool_ms,
        render_ms=render_ms,
        estimated_input_tokens=decision.estimated_input_tokens,
        estimated_output_tokens=decision.estimated_output_tokens,
        planner_model=decision.planner_model,
        error_class=error_class or decision.error_class,
    )


def _fallback_result(
    *,
    reason_code: str,
    scope_mode: str,
    project_ids: tuple[str, ...],
    started: float,
    trace_context: TraceContext,
    surface: str,
    operation: str | None = None,
    planner_ms: float = 0.0,
    tool_ms: float = 0.0,
    render_ms: float = 0.0,
    estimated_input_tokens: int = 0,
    estimated_output_tokens: int = 0,
    planner_model: str | None = None,
    error_class: str | None = None,
) -> ToolCentricServingResult:
    fallback = ToolCentricServingResult(
        outcome="fallback",
        reason_code=reason_code,
        scope_mode=scope_mode,
        project_ids=project_ids,
        operation=operation,
        planner_ms=planner_ms,
        tool_ms=tool_ms,
        render_ms=render_ms,
        elapsed_ms=(time.monotonic() - started) * 1000,
        estimated_input_tokens=estimated_input_tokens,
        estimated_output_tokens=estimated_output_tokens,
        planner_model=planner_model,
        error_class=error_class,
    )
    _safe_event(
        trace_context,
        "assistant.tool_centric_serving",
        status="skipped" if not error_class else "error",
        duration_ms=fallback.elapsed_ms,
        metadata={**fallback.safe_metadata(), "surface": surface},
    )
    return fallback


def _validate_tool_result(
    result: Any,
    *,
    trusted_scope_mode: str,
    trusted_project_ids: tuple[str, ...],
    operation: str,
    sources: list[dict[str, Any]],
) -> str | None:
    if not isinstance(result, dict):
        return "typed tool returned a non-object result"
    if result.get("operation") != operation:
        return "typed tool operation does not match the validated plan"
    scope = result.get("effective_scope")
    if not isinstance(scope, dict) or scope.get("mode") != trusted_scope_mode:
        return "typed tool result scope mode does not match trusted scope"
    result_ids = tuple(str(item) for item in (scope.get("project_ids") or []))
    if trusted_scope_mode == "projects" and result_ids != trusted_project_ids:
        return "typed tool result project scope does not match trusted scope"
    if trusted_scope_mode == "portfolio" and result_ids:
        return "portfolio result unexpectedly contains selected project scope"
    summary_source_id = str(result.get("summary_source_id") or "")
    known_sources = {str(item.get("source_id") or "") for item in sources}
    if not summary_source_id or summary_source_id not in known_sources:
        return "typed tool result has no trusted summary source"
    if operation == "list":
        rows = result.get("results")
        fields = result.get("fields")
        if not isinstance(rows, list) or not isinstance(fields, list):
            return "typed list result has an invalid shape"
        for row in rows:
            if not isinstance(row, dict):
                return "typed list result contains a non-object row"
            if not str(row.get("project_id") or ""):
                return "typed list row is missing project_id"
            if str(row.get("source_id") or "") not in known_sources:
                return "typed list row is missing a trusted source"
    elif operation == "count" and not isinstance(result.get("value"), int):
        return "typed count result has an invalid value"
    elif operation == "exists" and not isinstance(result.get("value"), bool):
        return "typed exists result has an invalid value"
    elif operation == "group" and not isinstance(result.get("groups"), list):
        return "typed group result has an invalid shape"
    return None


def _plan_payload(decision: ToolCentricRouteDecision) -> dict[str, Any] | None:
    return decision.plan.model_dump(mode="json") if decision.plan is not None else None


def _normalize_as_of(value: datetime | None) -> datetime:
    current = value or datetime.now(timezone.utc)
    return current if current.tzinfo is not None else current.replace(tzinfo=timezone.utc)


def _safe_event(context: TraceContext, event_type: str, **kwargs: Any) -> None:
    try:
        emit_event(context, event_type, **kwargs)
    except Exception:
        pass


__all__ = [
    "TOOL_CENTRIC_SERVING_VERSION",
    "ToolCentricServingResult",
    "should_attempt_tool_centric_serving",
    "should_run_tool_centric_shadow",
    "tool_centric_mode",
    "tool_centric_planner_model",
    "tool_centric_planner_timeout_seconds",
    "tool_centric_surface_enabled",
    "try_tool_centric_read",
]
