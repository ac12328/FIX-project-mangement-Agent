"""LiteLLM-backed Question Generator, Golden Generator, and Semantic Judge roles."""

from __future__ import annotations

import json
import re
from datetime import datetime
from typing import Any, Callable

from pydantic import BaseModel

from .artifacts import canonical_json
from .models import (
    AtomicClaim,
    EmailMiningSignal,
    EvalCase,
    EvidenceBundle,
    ForbiddenClaim,
    GoldenSpec,
    LLMCallMetadata,
    QuestionGenerationOutput,
    SemanticJudgeOutput,
)

StructuredCall = Callable[..., Any]
QUESTION_MINER_PROMPT_VERSION = "question-miner-v2-holdout"
QUESTION_PROMPT_VERSION = "qg-v1"
GOLDEN_PROMPT_VERSION = "golden-v1"
JUDGE_PROMPT_VERSION = "judge-v2"

_WORKSPACE_CAPABILITIES = [
    {
        "tool": "portfolio_attention",
        "use_for": "portfolio priority, aging, overdue, stale, follow-up, and this-week attention questions",
    },
    {
        "tool": "query_portfolio",
        "use_for": "listing/filtering all projects or comparing canonical current project rows",
    },
    {
        "tool": "search_project_parties",
        "use_for": "vendor, organisation, client, regulator, or counterparty relationships",
    },
    {
        "tool": "get_project_brief",
        "use_for": "one-project current status, recent changes, evidence, owner/ball, and next action",
    },
    {
        "tool": "search_project_timeline",
        "use_for": "chronological project email, activity, milestone, and evidence history",
    },
    {
        "tool": "list_project_work",
        "use_for": "project commitments, follow-ups, internal actions, due dates, and residual work",
    },
    {
        "tool": "list_unanswered_threads",
        "use_for": "inbound-last email threads with no newer outbound response across the trusted scope",
    },
    {
        "tool": "get_project_evidence_audit",
        "use_for": "authoritative current truth, update decisions, historical evidence, and conflicts",
    },
    {
        "tool": "assess_milestone_readiness",
        "use_for": "deterministic source-bound milestone criteria and readiness without applying changes",
    },
]


def _workspace_capabilities() -> list[dict[str, Any]]:
    """Return eval routing guidance with the exact public tool input schemas."""
    from src.tools.registry import get_openai_tools_by_name

    definitions = {
        item["function"]["name"]: item["function"]
        for item in get_openai_tools_by_name(
            frozenset(capability["tool"] for capability in _WORKSPACE_CAPABILITIES)
        )
    }
    capabilities: list[dict[str, Any]] = []
    for capability in _WORKSPACE_CAPABILITIES:
        definition = definitions[capability["tool"]]
        capabilities.append({
            **capability,
            "description": definition.get("description", ""),
            "input_schema": definition.get("parameters", {"type": "object", "properties": {}}),
        })
    return capabilities


def _default_caller() -> StructuredCall:
    from src.llm import llm_call_structured

    return llm_call_structured


def _invoke(
    role: str,
    prompt_version: str,
    messages: list[dict[str, str]],
    response_model: type[BaseModel],
    *,
    structured_call: StructuredCall | None = None,
    model_override: str | None = None,
    temperature: float = 0.0,
    timeout_sec: int | None = None,
) -> tuple[BaseModel, LLMCallMetadata]:
    caller = structured_call or _default_caller()
    response = caller(
        f"ai_workspace_eval_{role}",
        messages,
        response_model.model_json_schema(),
        model_override=model_override,
        temperature=temperature,
        timeout_sec=timeout_sec,
        return_metadata=True,
    )
    if hasattr(response, "data"):
        data = response.data
        model = str(getattr(response, "model", model_override or "unknown"))
        inference = getattr(response, "inference", None)
    else:
        data = response
        model = model_override or "mock-or-unreported"
        inference = None
    parsed = response_model.model_validate(data)
    metadata = LLMCallMetadata(
        role=role,
        model=model,
        prompt_version=prompt_version,
        inference_trace_id=getattr(inference, "trace_id", None),
        output_revision_id=getattr(inference, "output_revision_id", None),
    )
    return parsed, metadata



def mine_questions(
    *,
    email_mining_signals: list[EmailMiningSignal],
    as_of: datetime,
    batch_id: str,
    count: int = 12,
    excluded_prior_cases: list[dict[str, Any]] | None = None,
    additional_instructions: str | None = None,
    structured_call: StructuredCall | None = None,
    model_override: str | None = None,
) -> list[EvalCase]:
    """Mine PM evaluation questions from a bounded, redacted email corpus."""
    if not email_mining_signals:
        raise ValueError("email_mining_signals cannot be empty")
    signal_ids = {signal.signal_id for signal in email_mining_signals}
    prior_cases = excluded_prior_cases or []
    system = (
        "You are the AI Workspace eval Question Miner. Act as an experienced project manager reviewing "
        "a bounded, redacted corpus of real project-email signals. Mine realistic questions a PM would ask "
        "to understand portfolio risk, ownership, dependencies, unanswered communication, vendor/client "
        "relationships, timeline changes, readiness, and next actions. Use only the supplied email_mining_signals "
        "as inspiration and provenance. Do not generate answers, reveal source_signal_ids, quote email snippets, "
        "or include private email addresses in question wording. Every case must have a concise pm_intent, a "
        "specific pm_rationale, and at least one source_signal_id that exists in the supplied corpus. The selected "
        "expected_tool and evidence_tool_args must be sufficient to deterministically collect the evidence needed "
        "for a golden answer. Only use argument keys explicitly present in the tool input_schema. Prefer natural "
        "questions over tool-shaped wording. Use portfolio scope for cross-project questions and projects scope "
        "with explicit project_ids for project-specific questions. Do not enable web search. Treat "
        "excluded_prior_cases as an already-used development/regression set: do not repeat or closely paraphrase "
        "their questions or PM intents, even if a similar pattern appears in a different project. Prefer distinct "
        "decision needs and evidence patterns for this holdout. Return exactly the requested number of unique cases."
    )
    if additional_instructions:
        system += " Additional requirements for this batch: " + str(additional_instructions).strip()
    payload = {
        "as_of": as_of.isoformat(),
        "requested_count": count,
        "workspace_capabilities": _workspace_capabilities(),
        "excluded_prior_cases": prior_cases,
        "additional_instructions": str(additional_instructions or "").strip(),
        "email_mining_signals": [
            signal.model_dump(mode="json") for signal in email_mining_signals
        ],
    }
    output, metadata = _invoke(
        "question_miner",
        QUESTION_MINER_PROMPT_VERSION,
        [{"role": "system", "content": system}, {"role": "user", "content": canonical_json(payload)}],
        QuestionGenerationOutput,
        structured_call=structured_call,
        model_override=model_override,
        temperature=0.35,
        timeout_sec=180,
    )
    assert isinstance(output, QuestionGenerationOutput)
    if len(output.cases) != count:
        raise ValueError(f"Question Miner returned {len(output.cases)} cases; expected {count}")

    cases: list[EvalCase] = []
    for draft in output.cases:
        unknown = sorted(set(draft.source_signal_ids) - signal_ids)
        if not draft.source_signal_ids:
            raise ValueError(f"Question Miner case {draft.case_id} has no source_signal_ids")
        if unknown:
            raise ValueError(
                f"Question Miner case {draft.case_id} references unknown source signals: {', '.join(unknown)}"
            )
        cases.append(EvalCase(
            case_id=draft.case_id,
            lifecycle="mined",
            question=draft.question,
            scope_mode=draft.scope_mode,
            project_ids=draft.project_ids,
            expected_tool=draft.expected_tool,
            evidence_tool_args=draft.evidence_tool_args,
            expected_args_constraints=draft.expected_args_constraints,
            alternative_tool_plans=draft.alternative_tool_plans,
            as_of=as_of,
            tags=draft.tags,
            mining_batch_id=batch_id,
            pm_intent=draft.pm_intent,
            pm_rationale=draft.pm_rationale,
            source_signal_ids=draft.source_signal_ids,
            repetitions=3,
            minimum_passes=2,
            generator=metadata,
        ))
    return cases

def generate_questions(
    *,
    seed_context: dict[str, Any],
    as_of: datetime,
    count: int = 6,
    structured_call: StructuredCall | None = None,
    model_override: str | None = None,
) -> list[EvalCase]:
    """Use a real LLM role to propose diverse tool-answerable evaluation cases."""
    system = (
        "You are the AI Workspace eval Question Generator. Generate natural-language portfolio questions "
        "that require the deterministic read-only tools supplied below. Do not include answers or invent facts. "
        "Every case must include concrete evidence_tool_args and atomic argument constraints. Only use argument "
        "keys explicitly present in that tool's input_schema. The Workspace scope and frozen as_of timestamp are "
        "injected by the runtime, so never require them as public tool arguments unless the input_schema exposes them. "
        "Include ambiguity, negative-result scope safety, multi-project comparison, and current-row-versus-email "
        "evidence cases when possible."
    )
    user = json.dumps(
        {
            "requested_count": count,
            "as_of": as_of.isoformat(),
            "capabilities": _workspace_capabilities(),
            "seed_context": seed_context,
        },
        ensure_ascii=False,
    )
    output, metadata = _invoke(
        "question_generator",
        QUESTION_PROMPT_VERSION,
        [{"role": "system", "content": system}, {"role": "user", "content": user}],
        QuestionGenerationOutput,
        structured_call=structured_call,
        model_override=model_override,
        temperature=0.2,
    )
    assert isinstance(output, QuestionGenerationOutput)
    if len(output.cases) != count:
        raise ValueError(f"Question Generator returned {len(output.cases)} cases; expected {count}")
    return [
        EvalCase(
            case_id=draft.case_id,
            question=draft.question,
            scope_mode=draft.scope_mode,
            project_ids=draft.project_ids,
            expected_tool=draft.expected_tool,
            evidence_tool_args=draft.evidence_tool_args,
            expected_args_constraints=draft.expected_args_constraints,
            alternative_tool_plans=draft.alternative_tool_plans,
            as_of=as_of,
            tags=draft.tags,
            repetitions=3,
            minimum_passes=2,
            generator=metadata,
        )
        for draft in output.cases
    ]


def generate_golden(
    case: EvalCase,
    evidence: EvidenceBundle,
    *,
    structured_call: StructuredCall | None = None,
    model_override: str | None = None,
) -> GoldenSpec:
    """Generate an atomic rubric using only deterministic evidence and sources."""
    class GoldenPayload(BaseModel):
        golden_answer: str
        required_claims: list[AtomicClaim]
        forbidden_claims: list[ForbiddenClaim] = []
        acceptable_variants: list[str] = []
        judge_notes: str = ""

    system = (
        "You are the AI Workspace eval Golden Answer Generator. Use only tool_result and sources in the evidence. "
        "Never add portfolio facts from memory. Directly answer the question with exact [[source:<source_id>]] citations. "
        "Make required_claims atomic, but include only claims necessary to answer the user's question; do not require "
        "every field merely because it appears in the tool result. For attention or ranking questions, require the "
        "matching projects, scope summary, and decisive attention signals; require next_action only when the user asks "
        "what to do next. For zero results, cite the summary source and limit the conclusion to the exact filters. "
        "Prefer the current project row over email/activity and never treat planning/contact emails as completion evidence. "
        "Do not claim that approved-to-go-live means production launched."
    )
    payload = {
        "case": case.model_dump(mode="json", exclude={"content_hash", "generator"}),
        "evidence": evidence.model_dump(mode="json", exclude={"content_hash"}),
    }
    output, metadata = _invoke(
        "golden_generator",
        GOLDEN_PROMPT_VERSION,
        [{"role": "system", "content": system}, {"role": "user", "content": canonical_json(payload)}],
        GoldenPayload,
        structured_call=structured_call,
        model_override=model_override,
        temperature=0.0,
    )
    assert isinstance(output, GoldenPayload)
    return GoldenSpec(
        case_id=case.case_id,
        case_version=case.version,
        golden_answer=output.golden_answer,
        required_claims=output.required_claims,
        forbidden_claims=output.forbidden_claims,
        acceptable_variants=output.acceptable_variants,
        expected_tool=case.expected_tool,
        expected_args_constraints=case.expected_args_constraints,
        alternative_tool_plans=case.alternative_tool_plans,
        judge_notes=output.judge_notes,
        generated_by=metadata,
    )


def judge_candidate(
    case: EvalCase,
    evidence: EvidenceBundle,
    golden: GoldenSpec,
    candidate_payload: dict[str, Any],
    gate_payload: list[dict[str, Any]],
    *,
    structured_call: StructuredCall | None = None,
    model_override: str | None = None,
) -> tuple[SemanticJudgeOutput, str]:
    """Run a semantic judge; deterministic scoring and vetoes happen elsewhere."""
    system = (
        "You are the AI Workspace semantic judge. Evaluate only against the supplied evidence and atomic rubric. "
        "Do not repair the candidate or infer portfolio facts. Return exactly one result for every required claim_id. "
        "Check scope-safe negative answers, current-row precedence, and overclaims such as equating approved-to-go-live "
        "with an already completed production launch. Deterministic gates are authoritative and cannot be overruled."
    )
    payload = _compact_judge_payload(
        case,
        evidence,
        golden,
        candidate_payload,
        gate_payload,
    )
    last_error: Exception | None = None
    for attempt in range(2):
        try:
            output, metadata = _invoke(
                "semantic_judge",
                JUDGE_PROMPT_VERSION,
                [{"role": "system", "content": system}, {"role": "user", "content": canonical_json(payload)}],
                SemanticJudgeOutput,
                structured_call=structured_call,
                model_override=model_override,
                temperature=0.0,
                timeout_sec=180,
            )
            break
        except Exception as exc:
            last_error = exc
            if attempt:
                raise
    else:  # pragma: no cover - loop always returns or raises
        raise last_error or RuntimeError("semantic judge failed")
    assert isinstance(output, SemanticJudgeOutput)
    return output, metadata.model


def _compact_judge_payload(
    case: EvalCase,
    evidence: EvidenceBundle,
    golden: GoldenSpec,
    candidate_payload: dict[str, Any],
    gate_payload: list[dict[str, Any]],
) -> dict[str, Any]:
    """Build a bounded semantic-judge payload without full tool results."""
    answer = str(candidate_payload.get("answer") or "")
    cited_source_ids = set(re.findall(
        r"\[\[source:([A-Za-z0-9][A-Za-z0-9_.:-]*)\]\]",
        answer,
    ))
    for source in candidate_payload.get("source_parts") or []:
        if isinstance(source, dict) and source.get("source_id"):
            cited_source_ids.add(str(source["source_id"]))
    required_source_ids = {
        source_id
        for claim in golden.required_claims
        for source_id in claim.supporting_source_ids
    }
    included_source_ids = required_source_ids | cited_source_ids
    compact_sources = [
        {
            "source_id": source.source_id,
            "kind": source.kind,
            "project_id": source.project_id,
            "occurred_at": source.occurred_at.isoformat() if source.occurred_at else None,
            "title": source.title[:240],
            "snippet": source.snippet[:700],
        }
        for source in evidence.sources
        if source.source_id in included_source_ids
    ]
    compact_trace = []
    for item in candidate_payload.get("tool_trace") or []:
        if not isinstance(item, dict):
            continue
        compact_trace.append({
            "tool": item.get("tool"),
            "arguments": item.get("arguments") if isinstance(item.get("arguments"), dict) else {},
        })
    compact_gates = [
        {
            "gate_id": gate.get("gate_id"),
            "passed": gate.get("passed"),
            "reason": gate.get("reason"),
        }
        for gate in gate_payload
        if isinstance(gate, dict)
    ]
    return {
        "case": {
            "case_id": case.case_id,
            "question": case.question,
            "scope_mode": case.scope_mode,
            "project_ids": case.project_ids,
            "as_of": case.as_of.isoformat(),
        },
        "golden": {
            "golden_answer": golden.golden_answer,
            "required_claims": [claim.model_dump(mode="json") for claim in golden.required_claims],
            "forbidden_claims": [claim.model_dump(mode="json") for claim in golden.forbidden_claims],
            "acceptable_variants": golden.acceptable_variants,
            "judge_notes": golden.judge_notes,
        },
        "evidence": {
            "tool_name": evidence.tool_name,
            "sources": compact_sources,
        },
        "candidate": {
            "answer": answer,
            "tool_trace": compact_trace,
            "validation_errors": candidate_payload.get("validation_errors") or [],
        },
        "deterministic_gates": compact_gates,
    }
