"""Trace reconstruction helpers for tool-argument and evidence replay."""

from __future__ import annotations

import json
import re
import sqlite3
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from src.agent.tool_evidence_eval_models import (
    ToolCallEvidence,
    ToolEvidenceObservation,
    ToolEvidenceTraceIssue,
)
from src.observability.payload_crypto import decrypt_payload

_CITATION_RE = re.compile(r"\[\[source:([A-Za-z0-9][A-Za-z0-9_.:-]*)\]\]")
_SUCCESS_STATUSES = frozenset({"ok", "completed", "succeeded", "success"})


@dataclass
class _CallBuilder:
    tool_call_id: str
    tool_name: str = ""
    arguments: dict[str, Any] | None = None
    result: Any = None
    status: str = "unknown"
    argument_payload_available: bool = False
    result_payload_available: bool = False
    start_event_id: str | None = None
    terminal_event_id: str | None = None


def _mapping(raw: Any) -> dict[str, Any] | None:
    if hasattr(raw, "model_dump"):
        raw = raw.model_dump()
    if isinstance(raw, sqlite3.Row):
        raw = dict(raw)
    return dict(raw) if isinstance(raw, Mapping) else None


def _metadata(event: Mapping[str, Any], issues: list[ToolEvidenceTraceIssue]) -> dict[str, Any]:
    raw = event.get("metadata")
    if raw is None:
        raw = event.get("metadata_json")
    if raw is None:
        return {}
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except (TypeError, ValueError):
            issues.append(ToolEvidenceTraceIssue(
                code="invalid_metadata_json",
                severity="error",
                message="Trace metadata_json is not valid JSON.",
                event_id=str(event.get("event_id") or "") or None,
            ))
            return {}
    if not isinstance(raw, Mapping):
        issues.append(ToolEvidenceTraceIssue(
            code="invalid_metadata_shape",
            severity="error",
            message="Trace metadata must be an object.",
            event_id=str(event.get("event_id") or "") or None,
        ))
        return {}
    return dict(raw)


def _decode_payload(
    holder: Any,
    event: Mapping[str, Any],
    issues: list[ToolEvidenceTraceIssue],
) -> tuple[Any, bool]:
    envelope = holder
    if envelope is None and event.get("payload_ciphertext"):
        envelope = {
            "ciphertext": event.get("payload_ciphertext"),
            "nonce": event.get("payload_nonce"),
            "key_version": event.get("payload_key_version") or "v1",
            "payload_hash": event.get("payload_hash"),
        }
    if envelope is None:
        return None, False
    if isinstance(envelope, Mapping) and "ciphertext" in envelope:
        if not envelope.get("nonce"):
            issues.append(ToolEvidenceTraceIssue(
                code="incomplete_payload_envelope",
                severity="error",
                message="Encrypted trace payload is missing its nonce.",
                event_id=str(event.get("event_id") or "") or None,
                tool_call_id=str(event.get("tool_call_id") or "") or None,
            ))
            return None, False
        try:
            return decrypt_payload(
                str(envelope["ciphertext"]),
                str(envelope["nonce"]),
                str(envelope.get("key_version") or "v1"),
            ), True
        except Exception as exc:
            issues.append(ToolEvidenceTraceIssue(
                code="payload_decrypt_failed",
                severity="error",
                message=f"Encrypted trace payload could not be decrypted: {type(exc).__name__}.",
                event_id=str(event.get("event_id") or "") or None,
                tool_call_id=str(event.get("tool_call_id") or "") or None,
            ))
            return None, False
    return envelope, True


def _normalized_event(
    raw: Any,
    issues: list[ToolEvidenceTraceIssue],
) -> tuple[dict[str, Any], Any] | None:
    record = _mapping(raw)
    if record is None:
        issues.append(ToolEvidenceTraceIssue(
            code="invalid_event_shape",
            severity="error",
            message=f"Trace event must be an object, got {type(raw).__name__}.",
        ))
        return None
    nested = record.get("event")
    if "event_type" not in record and isinstance(nested, Mapping):
        return dict(nested), record.get("payload")
    return record, record.get("payload")


def _source_records(value: Any) -> tuple[tuple[str, ...], dict[str, str]]:
    source_ids: list[str] = []
    projects: dict[str, str] = {}

    def add(source_id: Any, project_id: Any = None) -> None:
        text = str(source_id or "").strip()
        if not text:
            return
        source_ids.append(text)
        project = str(project_id or "").strip()
        if project:
            projects.setdefault(text, project)

    def walk(item: Any) -> None:
        if isinstance(item, Mapping):
            add(item.get("source_id"), item.get("project_id"))
            for key, child in item.items():
                key_text = str(key)
                if key_text.endswith("_source_id") and key_text != "source_id":
                    add(child, item.get("project_id"))
                elif key_text == "source_ids" and isinstance(child, Sequence) and not isinstance(child, (str, bytes)):
                    for source_id in child:
                        add(source_id, item.get("project_id"))
                walk(child)
        elif isinstance(item, Sequence) and not isinstance(item, (str, bytes)):
            for child in item:
                walk(child)

    walk(value)
    return tuple(dict.fromkeys(source_ids)), projects


def _answer_text(value: Any) -> str:
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, Mapping):
        for key in ("answer", "reply", "text", "content", "final_output"):
            if key in value:
                text = _answer_text(value[key])
                if text:
                    return text
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
        parts = [_answer_text(item) for item in value]
        return "\n".join(part for part in parts if part).strip()
    return ""


def observation_from_tool_evidence_events(events: Iterable[Any]) -> ToolEvidenceObservation:
    """Reconstruct arguments, results, sources, and final answer from stored traces."""

    issues: list[ToolEvidenceTraceIssue] = []
    normalized: list[tuple[dict[str, Any], Any]] = []
    for raw in events:
        item = _normalized_event(raw, issues)
        if item is not None:
            normalized.append(item)
    normalized.sort(key=lambda item: int(item[0].get("sequence") or 0))

    run_ids = {str(event.get("run_id")) for event, _ in normalized if event.get("run_id")}
    trace_ids = {str(event.get("trace_id")) for event, _ in normalized if event.get("trace_id")}
    sequences = [event.get("sequence") for event, _ in normalized if event.get("sequence") is not None]
    if len(run_ids) > 1:
        issues.append(ToolEvidenceTraceIssue(
            code="mixed_run_ids", severity="error", message="Trace events contain multiple run IDs."
        ))
    if len(trace_ids) > 1:
        issues.append(ToolEvidenceTraceIssue(
            code="mixed_trace_ids", severity="error", message="Trace events contain multiple trace IDs."
        ))
    if len(sequences) != len(set(sequences)):
        issues.append(ToolEvidenceTraceIssue(
            code="duplicate_sequence", severity="error", message="Trace sequence values are not unique."
        ))

    calls: dict[str, _CallBuilder] = {}
    call_order: list[str] = []
    final_answers: list[str] = []
    finish_count = 0
    for event, holder in normalized:
        event_type = str(event.get("event_type") or "")
        event_id = str(event.get("event_id") or "") or None
        if event_type not in {"tool.start", "tool.end", "tool.error", "run.finish"}:
            continue
        payload, payload_available = _decode_payload(holder, event, issues)
        if event_type == "run.finish":
            finish_count += 1
            if not payload_available or not isinstance(payload, Mapping) or "final_output" not in payload:
                issues.append(ToolEvidenceTraceIssue(
                    code="missing_final_output_payload",
                    severity="error",
                    message="run.finish lacks reconstructible final_output payload.",
                    event_id=event_id,
                ))
                continue
            answer = _answer_text(payload.get("final_output"))
            if not answer:
                issues.append(ToolEvidenceTraceIssue(
                    code="empty_final_output",
                    severity="error",
                    message="run.finish final_output has no answer text.",
                    event_id=event_id,
                ))
            else:
                final_answers.append(answer)
            continue

        tool_call_id = str(event.get("tool_call_id") or "").strip()
        if not tool_call_id:
            issues.append(ToolEvidenceTraceIssue(
                code="missing_tool_call_id",
                severity="error",
                message=f"{event_type} is missing tool_call_id.",
                event_id=event_id,
            ))
            continue
        metadata = _metadata(event, issues)
        tool_name = str(metadata.get("tool_name") or event.get("tool_name") or "").strip()
        builder = calls.get(tool_call_id)
        if builder is None:
            builder = _CallBuilder(tool_call_id=tool_call_id, tool_name=tool_name)
            calls[tool_call_id] = builder
            call_order.append(tool_call_id)
        elif tool_name and builder.tool_name and tool_name != builder.tool_name:
            issues.append(ToolEvidenceTraceIssue(
                code="tool_name_mismatch",
                severity="error",
                message="Tool lifecycle events disagree on tool_name.",
                event_id=event_id,
                tool_call_id=tool_call_id,
            ))
        if tool_name:
            builder.tool_name = tool_name

        if event_type == "tool.start":
            if builder.start_event_id is not None:
                issues.append(ToolEvidenceTraceIssue(
                    code="duplicate_tool_start",
                    severity="error",
                    message="Tool call has more than one tool.start event.",
                    event_id=event_id,
                    tool_call_id=tool_call_id,
                ))
            builder.start_event_id = event_id
            if payload_available and isinstance(payload, Mapping) and isinstance(payload.get("arguments"), Mapping):
                builder.arguments = dict(payload["arguments"])
                builder.argument_payload_available = True
            else:
                issues.append(ToolEvidenceTraceIssue(
                    code="missing_tool_arguments_payload",
                    severity="error",
                    message="tool.start lacks reconstructible arguments payload.",
                    event_id=event_id,
                    tool_call_id=tool_call_id,
                ))
        else:
            if builder.terminal_event_id is not None:
                issues.append(ToolEvidenceTraceIssue(
                    code="duplicate_tool_terminal",
                    severity="error",
                    message="Tool call has more than one terminal event.",
                    event_id=event_id,
                    tool_call_id=tool_call_id,
                ))
            builder.terminal_event_id = event_id
            builder.status = str(event.get("status") or ("error" if event_type == "tool.error" else "ok"))
            if event_type == "tool.end" and payload_available and isinstance(payload, Mapping) and "result" in payload:
                builder.result = payload.get("result")
                builder.result_payload_available = True
            elif event_type == "tool.end":
                issues.append(ToolEvidenceTraceIssue(
                    code="missing_tool_result_payload",
                    severity="error",
                    message="tool.end lacks reconstructible result payload.",
                    event_id=event_id,
                    tool_call_id=tool_call_id,
                ))

    if finish_count == 0:
        issues.append(ToolEvidenceTraceIssue(
            code="missing_run_finish", severity="error", message="Trace has no run.finish event."
        ))
    elif finish_count > 1:
        issues.append(ToolEvidenceTraceIssue(
            code="duplicate_run_finish", severity="error", message="Trace has multiple run.finish events."
        ))
    for builder in calls.values():
        if builder.start_event_id is None:
            issues.append(ToolEvidenceTraceIssue(
                code="orphan_tool_terminal",
                severity="error",
                message="Tool terminal event has no matching tool.start.",
                tool_call_id=builder.tool_call_id,
            ))
        if builder.terminal_event_id is None:
            issues.append(ToolEvidenceTraceIssue(
                code="missing_tool_terminal",
                severity="error",
                message="tool.start has no matching terminal event.",
                tool_call_id=builder.tool_call_id,
            ))
        if not builder.tool_name:
            issues.append(ToolEvidenceTraceIssue(
                code="missing_tool_name",
                severity="error",
                message="Tool lifecycle does not identify tool_name.",
                tool_call_id=builder.tool_call_id,
            ))

    observations: list[ToolCallEvidence] = []
    available_sources: list[str] = []
    for tool_call_id in call_order:
        builder = calls[tool_call_id]
        source_ids, projects = _source_records(builder.result) if builder.result_payload_available else ((), {})
        if builder.status.casefold() in _SUCCESS_STATUSES:
            available_sources.extend(source_ids)
        observations.append(ToolCallEvidence(
            tool_call_id=tool_call_id,
            tool_name=builder.tool_name,
            arguments=builder.arguments,
            result=builder.result,
            status=builder.status,
            argument_payload_available=builder.argument_payload_available,
            result_payload_available=builder.result_payload_available,
            start_event_id=builder.start_event_id,
            terminal_event_id=builder.terminal_event_id,
            source_ids=source_ids,
            source_project_ids=projects,
        ))

    final_answer = final_answers[-1] if final_answers else ""
    cited = tuple(dict.fromkeys(_CITATION_RE.findall(final_answer)))
    return ToolEvidenceObservation(
        run_id=next(iter(run_ids), None) if len(run_ids) == 1 else None,
        trace_id=next(iter(trace_ids), None) if len(trace_ids) == 1 else None,
        tool_calls=tuple(observations),
        final_answer=final_answer,
        final_answer_available=bool(final_answer),
        cited_source_ids=cited,
        available_source_ids=tuple(dict.fromkeys(available_sources)),
        issues=tuple(issues),
    )


def load_tool_evidence_events(conn: sqlite3.Connection, run_id: str) -> list[dict[str, Any]]:
    cursor = conn.execute("SELECT * FROM trace_events WHERE run_id=? ORDER BY sequence", (run_id,))
    columns = [description[0] for description in cursor.description or ()]
    rows = [dict(zip(columns, row)) for row in cursor.fetchall()]
    if not rows:
        raise KeyError(run_id)
    return rows


def load_tool_evidence_spool(path: str | Path, run_id: str) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    with Path(path).open("r", encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, start=1):
            if not line.strip():
                continue
            try:
                record = json.loads(line)
            except json.JSONDecodeError as exc:
                raise ValueError(f"Invalid JSON on spool line {line_number}") from exc
            event = record.get("event") if isinstance(record, Mapping) else None
            if isinstance(event, Mapping) and str(event.get("run_id") or "") == run_id:
                records.append(record)
    if not records:
        raise KeyError(run_id)
    return records


__all__ = [
    "load_tool_evidence_events",
    "load_tool_evidence_spool",
    "observation_from_tool_evidence_events",
]
