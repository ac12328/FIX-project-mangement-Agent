"""Plaintext-in-memory orchestration for Policy-v4 offline replay.

The public API stays in this module while focused cache and preparation helpers
live in sibling modules. Decrypted payloads remain in memory only.
"""

from __future__ import annotations

import time
from typing import Any, Callable

from src.assistant import grounding
from src.assistant.evals.grounding_repair_certificate_v4 import (
    CertificateProfile,
    evaluate_grounding_repair_certificate_v4,
)
from src.assistant.evals.grounding_repair_v4_replay_cache import (
    ReplayCache,
    _citation_plan,
    _email_signals,
)
from src.assistant.evals.grounding_repair_v4_replay_prepare import (
    _prepare_answer_indexed,
    _validate_and_verified_partial_answer,
    _validate_answer,
    prepare_answer_for_replay,
)


def prime_replay_cache(payloads: list[dict[str, Any]], cache: ReplayCache) -> None:
    """Prime pure cross-case classifications outside per-case replay timing."""
    for payload in payloads:
        for item in payload.get("tool_results") or []:
            if not isinstance(item, dict) or item.get("tool") != "get_project_brief":
                continue
            result = item.get("result") if isinstance(item.get("result"), dict) else {}
            emails = (
                result.get("recent_emails")
                if isinstance(result.get("recent_emails"), list)
                else []
            )
            for email in emails:
                if isinstance(email, dict):
                    _email_signals(email, cache)


def _codes(errors: list[str]) -> tuple[str, ...]:
    return tuple(grounding._validation_error_code(item) for item in errors)


def replay_case_v4(
    payload: dict[str, Any],
    cache: ReplayCache,
    *,
    certificate_evaluator: Callable[..., Any] = evaluate_grounding_repair_certificate_v4,
) -> dict[str, Any]:
    """Replay one decrypted case and return parity plus plaintext-free timings."""
    sources = [item for item in payload.get("sources") or [] if isinstance(item, dict)]
    tool_results = [item for item in payload.get("tool_results") or [] if isinstance(item, dict)]
    prepared = str(payload.get("prepared_answer") or "")
    stored_candidate = str(payload.get("candidate_answer") or "")
    stored_initial = [str(item) for item in payload.get("initial_errors") or []]
    stored_candidate_errors = [str(item) for item in payload.get("candidate_errors") or []]

    known_sources = frozenset(str(source.get("source_id")) for source in sources)
    factual_tools_used = any(item.get("tool") != "think" for item in tool_results)
    citation_plan = _citation_plan(sources, cache)

    full_started = time.perf_counter_ns()
    started = full_started
    initial_errors, candidate = _validate_and_verified_partial_answer(
        prepared, known_sources, factual_tools_used
    )
    validate_initial_ms = (time.perf_counter_ns() - started) / 1_000_000

    started = time.perf_counter_ns()
    candidate = str(candidate)
    verified_partial_ms = (time.perf_counter_ns() - started) / 1_000_000

    started = time.perf_counter_ns()
    candidate = _prepare_answer_indexed(candidate, tool_results, cache, citation_plan)
    prepare_ms = (time.perf_counter_ns() - started) / 1_000_000

    started = time.perf_counter_ns()
    candidate_errors = _validate_answer(candidate, known_sources, factual_tools_used)
    validate_candidate_ms = (time.perf_counter_ns() - started) / 1_000_000

    profile = CertificateProfile()
    certificate_started = time.perf_counter_ns()
    decision = certificate_evaluator(
        prepared_answer=prepared,
        candidate_answer=candidate,
        initial_error_codes=_codes(initial_errors),
        candidate_error_codes=_codes(candidate_errors),
        sources=sources,
        factual_tools_used=factual_tools_used,
        profile=profile,
    )
    certificate_ms = (time.perf_counter_ns() - certificate_started) / 1_000_000
    full_replay_ms = (time.perf_counter_ns() - full_started) / 1_000_000
    return {
        "candidate_parity": candidate == stored_candidate,
        "initial_error_parity": initial_errors == stored_initial,
        "candidate_error_parity": candidate_errors == stored_candidate_errors,
        "would_skip_repair": decision.would_skip_repair,
        "certificate_ms": certificate_ms,
        "full_replay_ms": full_replay_ms,
        "stage_timings_ms": {
            "validate_initial": validate_initial_ms,
            "verified_partial": verified_partial_ms,
            "prepare": prepare_ms,
            "validate_candidate": validate_candidate_ms,
            "certificate": certificate_ms,
            "full_replay": full_replay_ms,
        },
    }


__all__ = [
    "ReplayCache",
    "prepare_answer_for_replay",
    "prime_replay_cache",
    "replay_case_v4",
]
