"""Final validation for replies after surface-specific grounding and transforms."""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from typing import Any

from src.agent.claims import format_claim_violation_reply, verify_claims


@dataclass(frozen=True)
class GroundedFinalizationResult:
    """Final reply and stable validation metadata for the serving surface."""

    text: str
    grounding_errors: tuple[str, ...] = ()
    claim_violation_codes: tuple[str, ...] = ()
    process_narrative_correction_codes: tuple[str, ...] = ()
    control_plane_acceptance_codes: tuple[str, ...] = ()


def _tool_history(loop_result: Any) -> list[tuple[str, ...]]:
    """Read a possibly partial loop result without trusting its exact type."""
    raw_history = getattr(loop_result, "tool_history", ()) or ()
    normalized: list[tuple[str, ...]] = []
    try:
        rounds = list(raw_history)
    except TypeError:
        return normalized
    for raw_round in rounds:
        if isinstance(raw_round, str):
            names = (raw_round,)
        else:
            try:
                names = tuple(str(name) for name in raw_round if name)
            except TypeError:
                continue
        if names:
            normalized.append(names)
    return normalized


def _execution_receipts(loop_result: Any) -> list[Any]:
    """Return only receipt-like objects that the claim verifier can consume."""
    raw_receipts = getattr(loop_result, "execution_receipts", ()) or ()
    try:
        receipts = list(raw_receipts)
    except TypeError:
        return []
    return [
        receipt
        for receipt in receipts
        if all(hasattr(receipt, field) for field in ("status", "outcome", "committed"))
    ]


def _append_guard_result(loop_result: Any, guard_result: dict[str, Any]) -> None:
    """Best-effort ledger update for typed and lightweight test loop results."""
    existing = getattr(loop_result, "guard_results", None)
    if isinstance(existing, list):
        existing.append(guard_result)
        return
    try:
        setattr(loop_result, "guard_results", [guard_result])
    except (AttributeError, TypeError):
        return


def _update_reply(loop_result: Any, reply: str) -> None:
    """Keep the authoritative loop reply aligned with the actually served text."""
    try:
        setattr(loop_result, "reply", reply)
    except (AttributeError, TypeError):
        return


def finalize_grounded_reply(
    candidate: str,
    *,
    loop_result: Any,
    conn: sqlite3.Connection | None,
    effective_project_id: str | None,
    surface: str,
    grounding_errors: tuple[str, ...] = (),
    enforce_claims: bool = True,
) -> GroundedFinalizationResult:
    """Validate the final transformed text against authoritative execution evidence.

    This must run after all deterministic cleanup and surface-specific grounding so
    no later transform can introduce an unsupported mutation-success claim.
    """
    candidate_text = str(candidate or "")
    original_candidate_text = candidate_text
    normalized_grounding_errors = tuple(str(error) for error in grounding_errors)
    receipts = _execution_receipts(loop_result)
    from src.agent.process_narrative_guard import correct_process_narrative

    narrative_result = correct_process_narrative(
        candidate_text,
        receipts=receipts,
    )
    from src.agent.control_plane_metrics import (
        control_plane_metrics,
        evaluate_control_plane_acceptance,
    )

    for code in narrative_result.correction_codes:
        control_plane_metrics.increment("narrative_correction", code=code)
    acceptance = evaluate_control_plane_acceptance(
        original_candidate_text,
        receipts=receipts,
    )
    for code in acceptance.violation_codes:
        control_plane_metrics.increment("acceptance_violation", code=code)
    candidate_text = narrative_result.text
    _append_guard_result(loop_result, {
        "guard": "receipt_grounded_process_narrative",
        "outcome": (
            "corrected" if narrative_result.correction_codes else "passed"
        ),
        "surface": str(surface),
        "correction_codes": list(narrative_result.correction_codes),
        "acceptance_violation_codes": list(acceptance.violation_codes),
    })
    violations = verify_claims(
        candidate_text,
        tool_history=_tool_history(loop_result),
        conn=conn,
        effective_project_id=effective_project_id,
        execution_receipts=receipts,
    )
    violation_codes = tuple(violation.code for violation in violations)

    if violations and enforce_claims:
        outcome = "blocked"
        final_text = format_claim_violation_reply(violations)
    elif violations:
        outcome = "observed_not_enforced"
        final_text = candidate_text
    elif enforce_claims:
        outcome = "passed"
        final_text = candidate_text
    else:
        outcome = "disabled"
        final_text = candidate_text

    _append_guard_result(loop_result, {
        "guard": "grounded_final_claim_validation",
        "outcome": outcome,
        "enabled": bool(enforce_claims),
        "surface": str(surface),
        "grounding_error_count": len(normalized_grounding_errors),
        "violation_count": len(violations),
        "violation_codes": list(violation_codes),
    })
    _update_reply(loop_result, final_text)

    return GroundedFinalizationResult(
        text=final_text,
        grounding_errors=normalized_grounding_errors,
        claim_violation_codes=violation_codes,
        process_narrative_correction_codes=narrative_result.correction_codes,
        control_plane_acceptance_codes=acceptance.violation_codes,
    )
