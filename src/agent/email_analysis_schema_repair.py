"""Bounded, privacy-safe schema repair for email analysis tool calls."""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from typing import Any

logger = logging.getLogger(__name__)


@dataclass
class EmailAnalysisSchemaRepairState:
    retry_count: int = 0
    pending: bool = False
    nudge_count: int = 0
    report_scheduled: bool = False
    legacy_arguments: dict[str, Any] | None = None


def append_schema_repair_nudge(
    state: EmailAnalysisSchemaRepairState,
    messages: list[dict[str, Any]],
    *,
    force_respond: bool,
) -> bool:
    """Add at most one nudge when the model stops before correcting the call."""
    if not state.pending or force_respond or state.nudge_count >= 1:
        return False
    state.nudge_count += 1
    messages.append({
        "role": "user",
        "content": (
            "<tool-schema-repair>Retry report_email_analysis exactly once. "
            "Correct only the schema paths returned by the tool, keep "
            "assessment_detail extraction-only, preserve exact bounded citations, "
            "and do not invent confidence values or evidence.</tool-schema-repair>"
        ),
    })
    return True


def _schema_diagnostics(arguments: dict[str, Any]) -> list[str]:
    """Validate the optional extraction detail without exposing argument values."""
    detail = arguments.get("assessment_detail")
    if detail is None:
        return []

    from pydantic import ValidationError
    from src.models.progress_assessment import ProjectStateAssessment
    from src.tools.registry import REPORT_EMAIL_ANALYSIS
    from src.tools.schema_validation import (
        json_schema_diagnostics,
        pydantic_validation_diagnostics,
    )

    detail_schema = REPORT_EMAIL_ANALYSIS.input_schema["properties"]["assessment_detail"]
    diagnostics = [
        f"assessment_detail.{item}" if item != "<root>" else "assessment_detail"
        for item in json_schema_diagnostics(detail, detail_schema)
    ]
    if diagnostics or not isinstance(detail, dict):
        return diagnostics or ["assessment_detail:type"]

    try:
        ProjectStateAssessment.model_validate(detail)
    except ValidationError as exc:
        return [
            f"assessment_detail.{item}"
            for item in pydantic_validation_diagnostics(exc)
        ][:12]
    except (TypeError, ValueError) as exc:
        return [f"assessment_detail:{type(exc).__name__}"]

    outer_project_id = str(arguments.get("project_id") or "").strip()
    detail_project_id = str(detail.get("project_id") or "").strip()
    if outer_project_id and detail_project_id != outer_project_id:
        diagnostics.append("assessment_detail.project_id:identity_mismatch")

    analysis_ref = str(arguments.get("analysis_ref") or "").strip()
    source_ref = str(detail.get("source_ref") or "").strip()
    if analysis_ref and source_ref != analysis_ref:
        diagnostics.append("assessment_detail.source_ref:identity_mismatch")
    return diagnostics[:12]


def _tool_message(
    tool_call: dict[str, Any],
    diagnostics: list[str],
    *,
    retryable: bool,
) -> dict[str, Any]:
    return {
        "role": "tool",
        "tool_call_id": tool_call.get("id", ""),
        "name": "report_email_analysis",
        "content": json.dumps({
            "ok": False,
            "error": "tool_arguments_schema_invalid",
            "tool": "report_email_analysis",
            "retryable": retryable,
            "retry_limit": 1,
            "validation_errors": diagnostics,
            "message": (
                "Correct the listed schema paths and retry exactly once. "
                "Do not omit assessment_detail, invent evidence, citations, or "
                "confidence values."
                if retryable else
                "The single schema-repair attempt has been used; no further "
                "report will be executed in this run."
            ),
        }, ensure_ascii=False),
    }


def _guard(
    tool_call: dict[str, Any],
    *,
    outcome: str,
    reason: str,
    diagnostics: list[str],
) -> dict[str, Any]:
    return {
        "guard": "tool_schema_validation",
        "tool_call_id": tool_call.get("id"),
        "tool_name": "report_email_analysis",
        "outcome": outcome,
        "reason": reason,
        "validation_errors": diagnostics,
    }


def prepare_email_analysis_tool_calls(
    state: EmailAnalysisSchemaRepairState,
    tool_calls: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
    """Gate malformed detail and allow one model-authored correction.

    The first malformed detail is not executed. A malformed retry is executed
    once with only ``assessment_detail`` removed, preserving the model's legacy
    summary/action while never fabricating evidence or confidence.
    """
    executable: list[dict[str, Any]] = []
    blocked_messages: list[dict[str, Any]] = []
    guards: list[dict[str, Any]] = []

    for tool_call in tool_calls:
        function_data = tool_call.get("function", {})
        if function_data.get("name") != "report_email_analysis":
            executable.append(tool_call)
            continue

        if state.report_scheduled:
            diagnostics = ["report_email_analysis:already_recorded"]
            blocked_messages.append(_tool_message(tool_call, diagnostics, retryable=False))
            guards.append(_guard(
                tool_call,
                outcome="blocked",
                reason="already_recorded",
                diagnostics=diagnostics,
            ))
            continue

        raw_args = function_data.get("arguments") or "{}"
        try:
            arguments = (
                json.loads(raw_args) if isinstance(raw_args, str) else dict(raw_args)
            )
        except (json.JSONDecodeError, TypeError, ValueError):
            diagnostics = ["<root>:invalid_json"]
            if state.retry_count == 0:
                state.retry_count = 1
                state.pending = True
                blocked_messages.append(_tool_message(tool_call, diagnostics, retryable=True))
                guards.append(_guard(
                    tool_call,
                    outcome="blocked",
                    reason="invalid_json_retry_requested",
                    diagnostics=diagnostics,
                ))
                logger.warning(
                    "Rejected invalid report_email_analysis JSON; validation_errors=%s",
                    ",".join(diagnostics),
                )
            else:
                state.pending = False
                state.report_scheduled = True
                if state.legacy_arguments is not None:
                    corrected_function = dict(function_data)
                    corrected_function["arguments"] = json.dumps(
                        state.legacy_arguments, ensure_ascii=False,
                    )
                    corrected_call = dict(tool_call)
                    corrected_call["function"] = corrected_function
                    executable.append(corrected_call)
                    guards.append(_guard(
                        tool_call,
                        outcome="fail_open_legacy",
                        reason="invalid_json_after_single_retry",
                        diagnostics=diagnostics,
                    ))
                    logger.warning(
                        "Email analysis JSON repair exhausted; preserving cached legacy "
                        "fields; validation_errors=%s",
                        ",".join(diagnostics),
                    )
                else:
                    blocked_messages.append(
                        _tool_message(tool_call, diagnostics, retryable=False)
                    )
                    guards.append(_guard(
                        tool_call,
                        outcome="blocked",
                        reason="invalid_json_after_single_retry",
                        diagnostics=diagnostics,
                    ))
                    logger.warning(
                        "Email analysis JSON repair exhausted; no parseable legacy payload "
                        "was available; validation_errors=%s",
                        ",".join(diagnostics),
                    )
            continue

        diagnostics = _schema_diagnostics(arguments)
        if state.pending and "assessment_detail" not in arguments:
            diagnostics = ["assessment_detail:missing"]

        if diagnostics and state.retry_count == 0:
            state.retry_count = 1
            state.pending = True
            state.legacy_arguments = dict(arguments)
            state.legacy_arguments.pop("assessment_detail", None)
            blocked_messages.append(_tool_message(tool_call, diagnostics, retryable=True))
            guards.append(_guard(
                tool_call,
                outcome="blocked",
                reason="schema_invalid_retry_requested",
                diagnostics=diagnostics,
            ))
            logger.warning(
                "Rejected invalid report_email_analysis detail; validation_errors=%s",
                ",".join(diagnostics),
            )
            continue

        corrected_call = tool_call
        if diagnostics:
            legacy_args = dict(arguments)
            legacy_args.pop("assessment_detail", None)
            corrected_function = dict(function_data)
            corrected_function["arguments"] = json.dumps(legacy_args, ensure_ascii=False)
            corrected_call = dict(tool_call)
            corrected_call["function"] = corrected_function
            guards.append(_guard(
                tool_call,
                outcome="fail_open_legacy",
                reason="schema_invalid_after_single_retry",
                diagnostics=diagnostics,
            ))
            logger.warning(
                "Email analysis schema repair exhausted; preserving legacy fields; "
                "validation_errors=%s",
                ",".join(diagnostics),
            )
        elif state.pending:
            guards.append(_guard(
                tool_call,
                outcome="corrected",
                reason="schema_valid_after_single_retry",
                diagnostics=[],
            ))

        state.pending = False
        state.report_scheduled = True
        state.legacy_arguments = None
        executable.append(corrected_call)

    return executable, blocked_messages, guards
