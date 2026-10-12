"""Independent reference and blinded judge calls for the A/B/R experiment."""

from __future__ import annotations

import hashlib
import json
import time
from typing import Any, Literal

from src.email.project_truth.contracts import (
    BlindJudgeResult,
    EmailProjectTruthCase,
    PipelineCallMetadata,
    ReferenceRecommendation,
)
from src.email.project_truth.extraction import milestone_contract_payload
from src.email.project_truth.model_routing import normalize_research_model
from src.llm.client import StructuredLLMResponse, get_trace_ids, llm_call_structured

_REFERENCE_PROMPT = """You are an independent senior FIX-onboarding PM constructing Reference R.
Use only the supplied bounded evidence, historical project snapshot, and authoritative milestone
contract. Do not assume planned, conditional, quoted-history, or forwarded-history work completed.
Protect terminal projects and abstain when identity, evidence, or required criteria are uncertain.
Recommend only reviewable project fields supported by the evidence. Copy supported source_ref values
exactly. Do not reveal hidden reasoning; return only the requested structured JSON."""

_JUDGE_PROMPT = """You are a strict blinded A/B evaluator. The two candidate answers are anonymous.
Compare each candidate to Reference R and the same frozen evidence. Penalize unsupported state
changes, invalid or absent support, omitted important distinctions, wrong historical scope, and poor
PM actionability. Do not reward length. Score exactly: factual correctness 0-35, citation support
0-25, completeness 0-20, scope/as-of correctness 0-10, clarity/actionability 0-10. A difference of
2 points or less is a tie. Return only the requested structured JSON."""


def reference_payload(case: EmailProjectTruthCase) -> dict[str, Any]:
    """Build the leakage-free input used for Reference R."""
    return {
        "case": {
            "project_id": case.project_id,
            "subject": case.subject,
            "sender_email": case.sender_email,
            "received_at": case.received_at.isoformat() if case.received_at else None,
            "quoted_history": case.quoted_history,
            "forwarded_attachment": case.forwarded_attachment,
        },
        "historical_project_snapshot": case.project_snapshot.model_dump(mode="json"),
        "bounded_sources": [source.model_dump(mode="json") for source in case.sources],
        "milestone_contract": milestone_contract_payload(),
    }


def _metadata(response: StructuredLLMResponse, started: float) -> PipelineCallMetadata:
    inference = response.inference
    return PipelineCallMetadata(
        model=response.model,
        latency_ms=int((time.perf_counter() - started) * 1000),
        trace_ids=get_trace_ids(),
        inference_trace_id=inference.trace_id if inference else None,
        output_revision_id=inference.output_revision_id if inference else None,
    )


def run_reference(
    case: EmailProjectTruthCase,
    *,
    model_override: str = "gpt-5.6-sol",
    timeout_sec: int = 180,
) -> tuple[ReferenceRecommendation, PipelineCallMetadata]:
    started = time.perf_counter()
    response = llm_call_structured(
        "reasoning",
        [
            {"role": "system", "content": _REFERENCE_PROMPT},
            {"role": "user", "content": json.dumps(reference_payload(case), ensure_ascii=False)},
        ],
        ReferenceRecommendation.model_json_schema(),
        model_override=normalize_research_model(model_override),
        temperature=0,
        timeout_sec=timeout_sec,
        return_metadata=True,
        notify_on_error=False,
    )
    if not isinstance(response, StructuredLLMResponse):
        raise TypeError("reference call did not return metadata")
    reference = ReferenceRecommendation.model_validate(response.data)
    allowed_refs = {source.source_ref for source in case.sources}
    reference.supported_source_refs = [
        source_ref for source_ref in reference.supported_source_refs if source_ref in allowed_refs
    ]
    return reference, _metadata(response, started)


def blind_assignment(experiment_id: str, case_id: str) -> dict[str, Literal["baseline", "candidate"]]:
    """Deterministically assign systems to anonymous answer slots."""
    digest = hashlib.sha256(f"{experiment_id}:{case_id}".encode("utf-8")).digest()
    if digest[0] % 2 == 0:
        return {"answer_a": "baseline", "answer_b": "candidate"}
    return {"answer_a": "candidate", "answer_b": "baseline"}


def judge_payload(
    case: EmailProjectTruthCase,
    reference: ReferenceRecommendation,
    answer_a: dict[str, Any],
    answer_b: dict[str, Any],
) -> dict[str, Any]:
    """Build a blind judge input without system identity or controlled oracle data."""
    return {
        "question": "What project-state updates, if any, does this inbound email support?",
        "historical_project_snapshot": case.project_snapshot.model_dump(mode="json"),
        "bounded_sources": [source.model_dump(mode="json") for source in case.sources],
        "reference_r": reference.model_dump(mode="json"),
        "answer_a": answer_a,
        "answer_b": answer_b,
    }


def run_blind_judge(
    payload: dict[str, Any],
    *,
    model_override: str = "gpt-5.6-terra",
    timeout_sec: int = 180,
) -> tuple[BlindJudgeResult, PipelineCallMetadata]:
    started = time.perf_counter()
    response = llm_call_structured(
        "reasoning",
        [
            {"role": "system", "content": _JUDGE_PROMPT},
            {"role": "user", "content": json.dumps(payload, ensure_ascii=False)},
        ],
        BlindJudgeResult.model_json_schema(),
        model_override=normalize_research_model(model_override),
        temperature=0,
        timeout_sec=timeout_sec,
        return_metadata=True,
        notify_on_error=False,
    )
    if not isinstance(response, StructuredLLMResponse):
        raise TypeError("judge call did not return metadata")
    return BlindJudgeResult.model_validate(response.data), _metadata(response, started)
