"""Bounded LLM planning and execution helpers for purpose-sized portfolio queries."""

from __future__ import annotations

import json
import sqlite3
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from src.assistant.portfolio_query_v2 import (
    PORTFOLIO_PROJECT_DETAIL_FIELDS,
    PORTFOLIO_QUERY_V2_JSON_SCHEMA,
    PortfolioQueryV2Request,
    portfolio_query_v2,
    render_portfolio_query_v2,
)
from src.llm.client import StructuredLLMResponse, _estimate_tokens, llm_call_structured
from src.observability.contracts import TraceContext, trace_context_scope
from src.observability.lifecycle import emit_event, emit_run_finish, emit_run_start

_PLANNER_VERSION = "portfolio-query-v2-planner-v4"
_PLANNER_PROMPT = """You are a query planner for a FIX-onboarding portfolio database.
Translate the user's information need into exactly one purpose-sized typed query. Return data, not
prose. Do not write SQL. Do not broaden the trusted Workspace scope. Use typed mode whenever one
query can answer mechanically; use synthesis only when the answer genuinely requires interpretation
beyond count/list/exists/group. Use clarification when the request could fit this tool but wording is materially ambiguous, including
relative dates without an unambiguous convention. Use unsupported when the request is outside this tool's
portfolio project-record capabilities. A short reason may describe the observable decision, but must not
contain private chain-of-thought.

Semantics:
- Filters are AND across fields and OR within each list.
- project_id is an exact, case-insensitive identifier filter. A question asking whether a named
  project ID exists is a supported typed exists query using filters.project_id. Project IDs are
  identifiers, not client-name searches. Include terminal projects for project-ID existence unless
  the user explicitly narrows status or asks only about current/non-terminal projects.
- FIX conformance case IDs such as A001-A007, T001-T011, and S002 are not project IDs. Those
  lookups are wiki/knowledge questions; use unsupported.
- include_terminal=false excludes Completed, Archived, and Cancelled only when project_status is not
  explicitly filtered. Set include_terminal=true for full/entire/all-project portfolio questions or
  grouping that must include terminal projects. When project_status is explicitly filtered, leave
  include_terminal=false because the status filter itself controls terminal selection.
- Trusted selected-project scope only limits the accessible project universe. It does not imply that
  terminal projects should be included; follow the user's terminal wording exactly.
- market is normalized token membership over comma-separated stored values (for example HK matches
  both 'HK, US' and 'US,JP,HK').
- target dates use inclusive YYYY-MM-DD bounds.
- A request asking which projects became/completed/changed to a status within a time range is a
  historical transition query, not an updated_at or target date query. Use filters.transition with
  field=project_status, the requested to_values, and inclusive absolute effective_from/effective_to
  dates resolved from the supplied current_date/as_of. For example, "projects completed within the
  last 30 days" uses a project_status transition to Completed over that resolved date range. Include
  project_id, client_name, project_status, and transitioned_at for a list result. Do not use a current
  project_status filter as a substitute for the historical transition.
- client_name is a case-insensitive substring/contains filter, not an exact-name filter. A client-name
  lookup searches all visible project records, including terminal records, unless the user explicitly
  narrows it to current, unfinished, non-terminal, or a named status.
- next_action is the stored operator action. Requests such as "next action", "next step", "what do I
  need to do", "what should I do", and equivalent Chinese or Cantonese wording must select
  next_action. Never substitute current_milestone or ball_with for next_action.
- A request for project detail, details, overview, or equivalent Chinese wording is a list query whose
  visible core profile is project_id, client_name, project_status, current_milestone, ball_with,
  market, next_action, and target_go_live.
- A generic request to check, review, or show a named project asks for project detail, not existence.
  Use exists only when the user explicitly asks whether the project exists or is in the system.
- list fields are the visible output projection. Always include project_id and client_name, then only
  fields explicitly requested or needed to substantiate the selected condition. Never add unrelated
  market, date, milestone, status, or ownership fields.
- For list ordering, use project_id ascending unless the user explicitly asks for another order or the
  principal request is a chronological sequence. Use limit=50 unless the user requests a page size.
- For count, exists, and group operations, fields and sort must be empty arrays and cursor must be null.
- Descriptions such as current, unfinished, non-terminal, or live mean exclude terminal statuses; they
  do not mean Active-only unless Active is explicit.
- count returns only a number; exists returns a boolean; group returns counts; list returns projected rows.
- The current date and trusted as-of are supplied in the input. Resolve explicit relative dates into
  absolute filter dates. If wording is materially ambiguous, use clarification rather than guessing.
"""

_PLANNER_RESPONSE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "answer_mode": {"type": "string", "enum": ["typed", "synthesis", "clarification", "unsupported"]},
        "query": {"anyOf": [PORTFOLIO_QUERY_V2_JSON_SCHEMA, {"type": "null"}]},
        "language": {"type": "string", "enum": ["en", "zh-Hant"]},
        "reason": {"type": "string", "maxLength": 300},
    },
    "required": ["answer_mode", "query", "language", "reason"],
    "additionalProperties": False,
}


class PortfolioQueryV2Plan(BaseModel):
    """Validated observable planner decision."""

    model_config = ConfigDict(extra="forbid")

    answer_mode: Literal["typed", "synthesis", "clarification", "unsupported"]
    query: PortfolioQueryV2Request | None
    language: Literal["en", "zh-Hant"] = "en"
    reason: str = Field(max_length=300)

    @model_validator(mode="after")
    def validate_query_presence(self) -> "PortfolioQueryV2Plan":
        if self.answer_mode in {"unsupported", "clarification"} and self.query is not None:
            raise ValueError("unsupported and clarification plans cannot include a query")
        if self.answer_mode in {"typed", "synthesis"} and self.query is None:
            raise ValueError("typed and synthesis plans require a query")
        return self



_NEXT_ACTION_PHRASES = (
    "next action",
    "next step",
    "what do i need to do",
    "what should i do",
    "what i need to do",
    "what needs to be done",
    "\u4e0b\u4e00\u6b65",
    "\u4e0b\u6b65",
    "\u5f8c\u7e8c\u884c\u52d5",
    "\u5f8c\u7e8c\u4e8b\u9805",
    "\u540e\u7eed\u884c\u52a8",
    "\u540e\u7eed\u4e8b\u9879",
    "\u8981\u505a\u54a9",
    "\u8981\u505a\u4e5c",
    "\u9700\u8981\u505a\u4ec0\u9ebc",
    "\u9700\u8981\u505a\u4ec0\u4e48",
    "\u8ddf\u4f4f\u505a\u54a9",
)
_PROJECT_DETAIL_PHRASES = (
    "project detail",
    "project details",
    "project overview",
    "project information",
    "project info",
    "details of the project",
    "details for the project",
    "full details",
    "complete details",
    "\u5c08\u6848\u8a73\u60c5",
    "\u5c08\u6848\u8a73\u7d30\u8cc7\u6599",
    "\u5c08\u6848\u8cc7\u6599",
    "\u5c08\u6848\u6982\u6cc1",
    "\u9805\u76ee\u8a73\u60c5",
    "\u9805\u76ee\u8cc7\u6599",
    "\u9879\u76ee\u8be6\u60c5",
    "\u9879\u76ee\u8d44\u6599",
)
_PROJECT_NOUNS = ("project", "\u5c08\u6848", "\u9805\u76ee", "\u9879\u76ee")
_PROJECT_CHECK_TERMS = (
    "check",
    "review",
    "show",
    "inspect",
    "\u6aa2\u67e5",
    "\u68c0\u67e5",
    "\u67e5\u770b",
    "\u7747\u4e0b",
    "\u770b\u770b",
)
_EXPLICIT_EXISTENCE_PHRASES = (
    "check if",
    "check whether",
    " exist",
    "exists",
    "in the system",
    "in system",
    "in the database",
    "in database",
    "in the app",
    "in the application",
    "is there",
    "are there",
    "\u6709\u51c7",
    "\u6709\u7121",
    "\u6709\u65e0",
    "\u662f\u5426\u5b58\u5728",
    "\u5b58\u4e0d\u5b58\u5728",
    "\u7cfb\u7d71\u6709",
    "\u7cfb\u7edf\u6709",
)
_OTHER_PROJECT_FIELD_PHRASES = (
    "status",
    "milestone",
    "owner",
    "ownership",
    "ball",
    "market",
    "client",
    "customer",
    "target go-live",
    "target go live",
    "target date",
    "go-live",
    "go live",
    "deadline",
    "last update",
    "updated",
    "\u72c0\u614b",
    "\u72b6\u6001",
    "\u91cc\u7a0b\u7891",
    "\u8ddf\u9032\u65b9",
    "\u8ddf\u8fdb\u65b9",
    "\u8ca0\u8cac\u65b9",
    "\u8d1f\u8d23\u65b9",
    "\u5e02\u5834",
    "\u5e02\u573a",
    "\u5ba2\u6236",
    "\u5ba2\u6237",
    "\u76ee\u6a19\u4e0a\u7dda",
    "\u76ee\u6807\u4e0a\u7ebf",
    "\u4e0a\u7dda\u65e5\u671f",
    "\u4e0a\u7ebf\u65e5\u671f",
    "\u6700\u5f8c\u66f4\u65b0",
    "\u6700\u540e\u66f4\u65b0",
)


def _has_any_phrase(text: str, phrases: tuple[str, ...]) -> bool:
    return any(phrase in text for phrase in phrases)


def repair_portfolio_query_v2_plan(
    question: str,
    plan: PortfolioQueryV2Plan,
) -> PortfolioQueryV2Plan:
    """Repair high-confidence field intents after probabilistic planning."""
    if plan.query is None or plan.answer_mode not in {"typed", "synthesis"}:
        return plan

    text = " ".join(str(question or "").casefold().replace("\u2019", "'").split())
    wants_next_action = _has_any_phrase(text, _NEXT_ACTION_PHRASES)
    wants_details = _has_any_phrase(text, _PROJECT_DETAIL_PHRASES)
    wants_generic_check = (
        _has_any_phrase(text, _PROJECT_NOUNS)
        and _has_any_phrase(text, _PROJECT_CHECK_TERMS)
        and not _has_any_phrase(text, _EXPLICIT_EXISTENCE_PHRASES)
    )
    if not (wants_next_action or wants_details or wants_generic_check):
        return plan

    query_data = plan.query.model_dump(mode="json")
    query_data.update({"operation": "list", "group_by": None, "cursor": None})
    existing_fields = list(query_data.get("fields") or [])

    if wants_details or wants_generic_check:
        extras = [
            field
            for field in existing_fields
            if field not in PORTFOLIO_PROJECT_DETAIL_FIELDS
        ]
        query_data["fields"] = [*PORTFOLIO_PROJECT_DETAIL_FIELDS, *extras]
    elif wants_next_action and not _has_any_phrase(text, _OTHER_PROJECT_FIELD_PHRASES):
        query_data["fields"] = ["project_id", "client_name", "next_action"]
    else:
        repaired_fields = list(existing_fields)
        for field in ("project_id", "client_name", "next_action"):
            if field not in repaired_fields:
                repaired_fields.append(field)
        query_data["fields"] = repaired_fields

    repaired_query = PortfolioQueryV2Request.model_validate(query_data)
    if repaired_query == plan.query:
        return plan
    return plan.model_copy(update={"query": repaired_query})


@dataclass(frozen=True)
class PortfolioQueryV2Run:
    plan: PortfolioQueryV2Plan
    answer: str
    tool_result: dict[str, Any] | None
    sources: tuple[dict[str, Any], ...]
    elapsed_seconds: float
    planner_seconds: float
    tool_seconds: float
    render_seconds: float
    llm_calls: int
    estimated_input_tokens: int
    estimated_output_tokens: int
    planner_model: str | None
    trace_id: str
    run_id: str
    turn_id: str
    error: str | None = None


def plan_portfolio_query_v2(
    question: str,
    *,
    scope_mode: str,
    project_ids: list[str],
    as_of: datetime,
    previous_query: dict[str, Any] | None = None,
    model_override: str | None = None,
    timeout_sec: int = 90,
) -> tuple[PortfolioQueryV2Plan, dict[str, Any]]:
    """Ask one LLM call to express the information need in the strict v2 contract."""
    if as_of.tzinfo is None:
        as_of = as_of.replace(tzinfo=timezone.utc)
    payload = {
        "question": question,
        "trusted_scope": {
            "mode": scope_mode,
            "project_ids": list(project_ids) if scope_mode == "projects" else [],
        },
        "as_of": as_of.isoformat(),
        "current_date": as_of.date().isoformat(),
        "previous_query": previous_query,
    }
    messages = [
        {"role": "system", "content": _PLANNER_PROMPT},
        {"role": "user", "content": json.dumps(payload, ensure_ascii=False, sort_keys=True)},
    ]
    started = time.monotonic()
    response = llm_call_structured(
        "reasoning",
        messages,
        _PLANNER_RESPONSE_SCHEMA,
        model_override=model_override,
        temperature=0.0,
        timeout_sec=max(1, int(timeout_sec)),
        return_metadata=True,
        notify_on_error=False,
    )
    elapsed = time.monotonic() - started
    if not isinstance(response, StructuredLLMResponse):
        raise TypeError("portfolio_query_v2 planner did not return structured metadata")
    normalized_data = _normalize_planner_payload(response.data)
    plan = repair_portfolio_query_v2_plan(
        question,
        PortfolioQueryV2Plan.model_validate(normalized_data),
    )
    output_tokens = max(1, int(len(json.dumps(response.data, ensure_ascii=False)) / 3.5))
    return plan, {
        "elapsed_seconds": elapsed,
        "estimated_input_tokens": _estimate_tokens(messages),
        "estimated_output_tokens": output_tokens,
        "model": response.model,
        "planner_version": _PLANNER_VERSION,
    }


def _normalize_planner_payload(value: dict[str, Any]) -> dict[str, Any]:
    """Remove output-only list controls from aggregate operations before validation."""
    normalized = dict(value)
    query = normalized.get("query")
    if not isinstance(query, dict):
        return normalized
    normalized_query = dict(query)
    if normalized_query.get("operation") != "list":
        normalized_query["fields"] = []
        normalized_query["sort"] = []
        normalized_query["cursor"] = None
    normalized["query"] = normalized_query
    return normalized


def run_portfolio_query_v2_agent(
    conn: sqlite3.Connection,
    *,
    question: str,
    scope_mode: str = "portfolio",
    project_ids: list[str] | None = None,
    as_of: datetime | None = None,
    previous_query: dict[str, Any] | None = None,
    model_override: str | None = None,
) -> PortfolioQueryV2Run:
    """Run planner -> deterministic tool -> typed renderer in a research-only trace."""
    project_ids = list(project_ids or [])
    current = as_of or datetime.now(timezone.utc)
    if current.tzinfo is None:
        current = current.replace(tzinfo=timezone.utc)
    trace_context = TraceContext.create(surface="evaluation", task="portfolio_query_v2")
    total_started = time.monotonic()
    planner_meta: dict[str, Any] = {}
    sources: list[dict[str, Any]] = []
    plan: PortfolioQueryV2Plan | None = None
    result: dict[str, Any] | None = None
    answer = ""
    tool_seconds = 0.0
    render_seconds = 0.0
    error: str | None = None

    with trace_context_scope(trace_context):
        emit_run_start(
            trace_context,
            metadata={
                "surface": "evaluation",
                "task": "portfolio_query_v2",
                "planner_version": _PLANNER_VERSION,
                "scope_mode": scope_mode,
                "project_count": len(project_ids),
                "production_routing_changed": False,
            },
            payload={"question": question},
        )
        try:
            plan, planner_meta = plan_portfolio_query_v2(
                question,
                scope_mode=scope_mode,
                project_ids=project_ids,
                as_of=current,
                previous_query=previous_query,
                model_override=model_override,
            )
            emit_event(
                trace_context,
                "planner.decision",
                status="ok",
                duration_ms=float(planner_meta["elapsed_seconds"]) * 1000,
                metadata={
                    "answer_mode": plan.answer_mode,
                    "operation": plan.query.operation if plan.query else None,
                    "planner_version": _PLANNER_VERSION,
                    "model": planner_meta["model"],
                    "estimated_input_tokens": planner_meta["estimated_input_tokens"],
                    "estimated_output_tokens": planner_meta["estimated_output_tokens"],
                },
                payload={"plan": plan.model_dump(mode="json")},
            )
            if plan.answer_mode == "unsupported":
                answer = _unsupported_answer(plan.language)
            elif plan.answer_mode == "clarification":
                answer = _clarification_answer(plan.language)
            else:
                tool_started = time.monotonic()
                result = portfolio_query_v2(
                    conn,
                    {
                        **plan.query.model_dump(mode="json"),
                        "_assistant_scope_mode": scope_mode,
                        "_assistant_scope_project_ids": project_ids,
                        "_assistant_as_of": current.isoformat(),
                        "_assistant_source_sink": sources,
                    },
                )
                tool_seconds = time.monotonic() - tool_started
                emit_event(
                    trace_context,
                    "tool.end",
                    status="ok",
                    duration_ms=tool_seconds * 1000,
                    metadata={
                        "tool_name": "portfolio_query_v2",
                        "operation": result.get("operation"),
                        "result_bytes": len(json.dumps(result, ensure_ascii=False, default=str).encode("utf-8")),
                        "source_count": len(sources),
                    },
                    payload={
                        "arguments": plan.query.model_dump(mode="json"),
                        "result": result,
                        "sources": sources,
                    },
                )
                render_started = time.monotonic()
                if plan.answer_mode == "typed":
                    answer = render_portfolio_query_v2(result, language=plan.language)
                else:
                    answer = _synthesize_result(
                        question,
                        result,
                        sources,
                        language=plan.language,
                        model_override=model_override,
                    )
                render_seconds = time.monotonic() - render_started
            emit_run_finish(
                trace_context,
                status="completed",
                exit_reason="typed_result" if plan.answer_mode == "typed" else plan.answer_mode,
                final_output=answer,
                persistence_status="not_requested",
                metadata={
                    "llm_call_count": 1 if plan.answer_mode != "synthesis" else 2,
                    "tool_called": result is not None,
                    "source_count": len(sources),
                },
            )
        except Exception as exc:
            error = f"{type(exc).__name__}: {exc}"
            emit_run_finish(
                trace_context,
                status="error",
                exit_reason=type(exc).__name__,
                persistence_status="not_requested",
                anomaly_flags=[type(exc).__name__],
            )

    if plan is None:
        plan = PortfolioQueryV2Plan(
            answer_mode="unsupported",
            query=None,
            language="en",
            reason="Planner execution failed.",
        )
    llm_calls = 1 if planner_meta else 0
    if plan.answer_mode == "synthesis" and result is not None and not error:
        llm_calls += 1
    return PortfolioQueryV2Run(
        plan=plan,
        answer=answer,
        tool_result=result,
        sources=tuple(sources),
        elapsed_seconds=time.monotonic() - total_started,
        planner_seconds=float(planner_meta.get("elapsed_seconds") or 0.0),
        tool_seconds=tool_seconds,
        render_seconds=render_seconds,
        llm_calls=llm_calls,
        estimated_input_tokens=int(planner_meta.get("estimated_input_tokens") or 0),
        estimated_output_tokens=int(planner_meta.get("estimated_output_tokens") or 0),
        planner_model=planner_meta.get("model"),
        trace_id=trace_context.trace_id,
        run_id=trace_context.run_id,
        turn_id=str(trace_context.turn_id),
        error=error,
    )


def _unsupported_answer(language: str) -> str:
    if language == "zh-Hant":
        return "呢個問題超出 portfolio_query_v2 目前可安全回答的專案記錄範圍。"
    return "This question is outside the project-record scope that portfolio_query_v2 can answer safely."


def _clarification_answer(language: str) -> str:
    if language == "zh-Hant":
        return "???????????????????????????"
    return "This request is ambiguous; please provide the exact date or clarify the constraint to use."


def _synthesize_result(
    question: str,
    result: dict[str, Any],
    sources: list[dict[str, Any]],
    *,
    language: str,
    model_override: str | None,
) -> str:
    source_ids = [str(item["source_id"]) for item in sources]
    prompt = """Answer only from the supplied typed tool result and sources. Cite factual claims
with exact [[source:source_id]] identifiers. Do not invent facts. Keep the answer concise. Return
plain text, not JSON."""
    messages = [
        {"role": "system", "content": prompt},
        {
            "role": "user",
            "content": json.dumps(
                {
                    "question": question,
                    "language": language,
                    "allowed_source_ids": source_ids,
                    "tool_result": result,
                    "sources": sources,
                },
                ensure_ascii=False,
                sort_keys=True,
            ),
        },
    ]
    from src.llm.client import llm_call

    return str(
        llm_call(
            "reasoning",
            messages,
            model_override=model_override,
            temperature=0.0,
            timeout_sec=90,
        )
        or ""
    ).strip()


__all__ = [
    "PortfolioQueryV2Plan",
    "PortfolioQueryV2Run",
    "plan_portfolio_query_v2",
    "repair_portfolio_query_v2_plan",
    "run_portfolio_query_v2_agent",
]
