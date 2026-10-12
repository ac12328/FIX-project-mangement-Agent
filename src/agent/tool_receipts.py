"""Authoritative execution receipts for model-driven tool calls."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import asdict, dataclass, field, is_dataclass
import hashlib
from typing import Any


@dataclass(frozen=True)
class ToolEvidence:
    """Bounded evidence metadata retained across timeout and delegation boundaries."""

    source_type: str
    locator: str
    authority: str = "reported"
    status: str | None = None
    content_hash: str | None = None
    excerpt: str | None = None
    claim_coverage: tuple[str, ...] = field(default_factory=tuple)


@dataclass(frozen=True)
class ToolExecutionReceipt:
    tool_call_id: str
    tool_name: str
    status: str  # succeeded | failed | denied
    outcome: str
    committed: bool = False
    project_id: str | None = None
    operation_id: str | None = None
    error_code: str | None = None
    child_receipts: tuple["ToolExecutionReceipt", ...] = field(default_factory=tuple)
    evidence: tuple[ToolEvidence, ...] = field(default_factory=tuple)
    execution_started: bool = True
    cache_hit: bool = False

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)



def _coerce_result_mapping(result: Any) -> dict[str, Any] | None:
    """Return a plain mapping for dict, dataclass, or Pydantic-style results."""
    if isinstance(result, Mapping):
        return dict(result)
    if is_dataclass(result) and not isinstance(result, type):
        converted = asdict(result)
        return dict(converted) if isinstance(converted, Mapping) else None
    model_dump = getattr(result, "model_dump", None)
    if callable(model_dump):
        try:
            converted = model_dump(mode="python")
        except TypeError:
            converted = model_dump()
        return dict(converted) if isinstance(converted, Mapping) else None
    legacy_dict = getattr(result, "dict", None)
    if callable(legacy_dict):
        converted = legacy_dict()
        return dict(converted) if isinstance(converted, Mapping) else None
    return None


def _reported_failure(result: dict[str, Any]) -> tuple[bool, str, str | None]:
    """Classify explicit handler failure signals into receipt status/outcome/code."""
    raw_status = str(result.get("status") or "").strip().casefold()
    denied = raw_status in {"denied", "rejected", "unauthorized", "forbidden"}
    status_failed = raw_status in {
        "failed", "failure", "error", "timed_out", "timeout", "cancelled", "canceled",
        "denied", "rejected", "unauthorized", "forbidden",
    }
    error = result.get("error")
    failed = bool(
        error
        or result.get("ok") is False
        or result.get("success") is False
        or result.get("verified") is False
        or status_failed
    )
    if not failed:
        return False, "succeeded", None

    code_value = result.get("error_code") or result.get("code") or error
    if code_value is None:
        if result.get("verified") is False:
            code_value = "unverified_result"
        else:
            code_value = "tool_reported_failure"
    return True, "denied" if denied else "failed", str(code_value)


def _execution_started(result: dict[str, Any]) -> bool:
    """Distinguish handler invocation from deterministic pre-execution blocks."""
    explicit = result.get("execution_started")
    if isinstance(explicit, bool):
        return explicit
    return result.get("side_effect_status") != "not_started"


def _bounded_excerpt(value: Any, *, limit: int = 6000) -> str | None:
    text = str(value or "").strip()
    if not text:
        return None
    return text if len(text) <= limit else text[:limit].rstrip() + "..."


def _tool_evidence_from_mapping(raw: Mapping[str, Any]) -> ToolEvidence | None:
    locator = str(raw.get("locator") or raw.get("path") or "").strip()
    source_type = str(raw.get("source_type") or raw.get("type") or "reported").strip()
    if not locator:
        return None
    coverage = raw.get("claim_coverage") or ()
    if isinstance(coverage, str):
        coverage = (coverage,)
    else:
        try:
            coverage = tuple(str(item) for item in coverage if item)
        except TypeError:
            coverage = ()
    return ToolEvidence(
        source_type=source_type or "reported",
        locator=locator,
        authority=str(raw.get("authority") or "reported"),
        status=str(raw.get("status")) if raw.get("status") is not None else None,
        content_hash=(
            str(raw.get("content_hash")) if raw.get("content_hash") is not None else None
        ),
        excerpt=_bounded_excerpt(raw.get("excerpt")),
        claim_coverage=coverage,
    )


def _evidence_from_result(tool_name: str, result: dict[str, Any]) -> tuple[ToolEvidence, ...]:
    """Extract bounded, non-secret evidence needed for partial-result salvage."""
    records: list[ToolEvidence] = []
    raw_records = result.get("evidence_records") or result.get("evidence") or []
    if isinstance(raw_records, (list, tuple)):
        for raw in raw_records:
            if isinstance(raw, Mapping):
                record = _tool_evidence_from_mapping(raw)
                if record is not None:
                    records.append(record)

    if tool_name == "wiki_read":
        locator = str(result.get("path") or "").strip()
        body = str(result.get("body") or "")
        if locator and body:
            status = str(result.get("status") or "").strip()
            records.append(ToolEvidence(
                source_type="wiki",
                locator=locator,
                authority="canonical" if status.casefold() == "trusted" else "curated",
                status=status or None,
                content_hash=hashlib.sha256(body.encode("utf-8")).hexdigest(),
                excerpt=_bounded_excerpt(body),
                claim_coverage=("full_document",),
            ))

    deduped: dict[tuple[str, str, str | None], ToolEvidence] = {}
    for record in records:
        deduped[(record.source_type, record.locator, record.content_hash)] = record
    return tuple(deduped.values())


def _child_receipts_from_result(result: dict[str, Any]) -> tuple[ToolExecutionReceipt, ...]:
    raw_receipts = result.get("execution_receipts") or []
    receipts: list[ToolExecutionReceipt] = []
    if not isinstance(raw_receipts, list):
        return ()
    for raw in raw_receipts:
        if not isinstance(raw, dict):
            continue
        nested = _child_receipts_from_result({
            "execution_receipts": raw.get("child_receipts") or [],
        })
        try:
            evidence = tuple(
                record
                for item in (raw.get("evidence") or [])
                if isinstance(item, Mapping)
                for record in [_tool_evidence_from_mapping(item)]
                if record is not None
            )
            receipts.append(ToolExecutionReceipt(
                tool_call_id=str(raw.get("tool_call_id") or ""),
                tool_name=str(raw.get("tool_name") or ""),
                status=str(raw.get("status") or "failed"),
                outcome=str(raw.get("outcome") or "failed"),
                committed=bool(raw.get("committed")),
                project_id=raw.get("project_id"),
                operation_id=raw.get("operation_id"),
                error_code=raw.get("error_code"),
                child_receipts=nested,
                evidence=evidence,
                execution_started=bool(raw.get("execution_started", True)),
                cache_hit=bool(raw.get("cache_hit", False)),
            ))
        except (TypeError, ValueError):
            continue
    return tuple(receipts)


def _last_receipt(
    receipts: tuple[ToolExecutionReceipt, ...],
    predicate: Any,
) -> ToolExecutionReceipt | None:
    for receipt in reversed(receipts):
        if predicate(receipt):
            return receipt
    return None


def receipt_from_result(
    *,
    tool_call_id: str,
    tool_name: str,
    result: Any,
    project_id: str | None = None,
) -> ToolExecutionReceipt:
    """Build a trusted receipt from the handler result, never model prose."""
    result_mapping = _coerce_result_mapping(result)
    if result_mapping is None:
        return ToolExecutionReceipt(
            tool_call_id=tool_call_id,
            tool_name=tool_name,
            status="succeeded",
            outcome="completed",
            project_id=project_id,
        )
    result = result_mapping

    evidence = _evidence_from_result(tool_name, result)
    child_receipts = _child_receipts_from_result(result) if tool_name == "delegate" else ()
    child_committed = _last_receipt(
        child_receipts,
        lambda receipt: receipt.status == "succeeded" and receipt.committed,
    )
    child_staged = _last_receipt(
        child_receipts,
        lambda receipt: receipt.status == "succeeded" and receipt.outcome == "staged",
    )
    child_failed = _last_receipt(
        child_receipts,
        lambda receipt: receipt.status in {"failed", "denied"},
    )

    failed, failure_status, failure_code = _reported_failure(result)
    if failed:
        return ToolExecutionReceipt(
            tool_call_id=tool_call_id,
            tool_name=tool_name,
            status=failure_status,
            outcome=str(
                result.get("outcome")
                or result.get("completion_status")
                or failure_code
                or "failed"
            ),
            committed=bool(child_committed),
            project_id=(
                result.get("project_id")
                or (child_committed.project_id if child_committed else None)
                or project_id
            ),
            operation_id=(
                result.get("operation_id")
                or (child_committed.operation_id if child_committed else None)
            ),
            error_code=failure_code,
            child_receipts=child_receipts,
            evidence=evidence,
            execution_started=_execution_started(result),
            cache_hit=bool(result.get("cache_hit", False)),
        )

    outcome = str(
        result.get("outcome")
        or result.get("completion_status")
        or result.get("status")
        or "completed"
    )
    committed = bool(result.get("committed"))
    if tool_name == "delegate":
        if child_committed:
            committed = True
            outcome = child_committed.outcome
        elif child_staged:
            outcome = "staged"
        elif child_failed:
            outcome = child_failed.outcome
    if tool_name == "confirm_update" and outcome in {"accepted", "updated", "completed"}:
        committed = True
    if tool_name in {"update_project", "update_project_batch", "create_project"}:
        committed = False
        if outcome == "completed":
            outcome = "staged"

    return ToolExecutionReceipt(
        tool_call_id=tool_call_id,
        tool_name=tool_name,
        status="succeeded",
        outcome=outcome,
        committed=committed,
        project_id=(
            result.get("project_id")
            or (child_committed.project_id if child_committed else None)
            or (child_staged.project_id if child_staged else None)
            or project_id
        ),
        operation_id=(
            result.get("operation_id")
            or (child_committed.operation_id if child_committed else None)
            or (child_staged.operation_id if child_staged else None)
            or result.get("session_id")
            or result.get("draft_id")
            or result.get("record_id")
        ),
        error_code=(
            str(result.get("error_code"))
            if result.get("error_code") is not None
            else None
        ),
        child_receipts=child_receipts,
        evidence=evidence,
        execution_started=_execution_started(result),
        cache_hit=bool(result.get("cache_hit", False)),
    )
