"""Deterministic retry and idempotency policy for tool execution."""

from __future__ import annotations

import hashlib
import json
import sqlite3
import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

_RETRYABLE_HTTP_STATUS = frozenset({429, 502, 503, 504})
_RETRYABLE_TEXT = (
    "temporarily unavailable",
    "temporary unavailable",
    "too many requests",
    "rate limit",
    "connection reset",
    "connection aborted",
)


@dataclass(frozen=True)
class RetryPlan:
    """Bounded retry plan derived from static contract metadata."""

    max_attempts: int
    backoff_seconds: tuple[float, ...]


def retry_plan(policy: str) -> RetryPlan:
    """Return a small deterministic retry budget; unknown values fail closed."""
    normalized = str(policy or "none").strip().casefold()
    if normalized == "once":
        return RetryPlan(max_attempts=2, backoff_seconds=(0.05,))
    if normalized == "exponential":
        return RetryPlan(max_attempts=3, backoff_seconds=(0.05, 0.1))
    return RetryPlan(max_attempts=1, backoff_seconds=())


def retry_delay(plan: RetryPlan, attempt_index: int, seed: str) -> float:
    """Return stable full jitter bounded by the configured backoff slot."""
    if attempt_index < 0 or attempt_index >= len(plan.backoff_seconds):
        return 0.0
    ceiling = max(0.0, float(plan.backoff_seconds[attempt_index]))
    if ceiling == 0:
        return 0.0
    digest = hashlib.sha256(
        f"{seed}:{attempt_index}".encode("utf-8")
    ).digest()
    fraction = int.from_bytes(digest[:8], "big") / float((1 << 64) - 1)
    return ceiling * fraction


def retry_reason(exc: BaseException) -> str | None:
    """Classify retryable transport failures without exposing exception values."""
    if isinstance(exc, (PermissionError, ValueError, TypeError, sqlite3.IntegrityError)):
        return None
    if isinstance(exc, sqlite3.OperationalError):
        message = str(exc).casefold()
        if "locked" in message or "busy" in message:
            return "sqlite_busy"
        return None
    if isinstance(exc, TimeoutError):
        return "timeout"
    if isinstance(exc, ConnectionError):
        return "connection"

    status_code = getattr(exc, "status_code", None)
    response = getattr(exc, "response", None)
    if status_code is None and response is not None:
        status_code = getattr(response, "status_code", None)
    try:
        normalized_status = int(status_code)
    except (TypeError, ValueError):
        normalized_status = None
    if normalized_status in _RETRYABLE_HTTP_STATUS:
        return f"http_{normalized_status}"

    class_name = type(exc).__name__.casefold()
    if "timeout" in class_name:
        return "timeout"
    message = str(exc).casefold()
    if any(marker in message for marker in _RETRYABLE_TEXT):
        return "temporary_transport"
    return None


def trusted_idempotency_key(
    tool_name: str,
    context: Mapping[str, Any],
) -> str:
    """Derive a handler key from trusted call identity, never model arguments."""
    explicit = context.get("idempotency_key")
    call_id = context.get("tool_call_id")
    identity = explicit if explicit not in (None, "") else call_id
    if identity in (None, ""):
        identity = uuid.uuid4().hex
    payload = json.dumps(
        {
            "tool": tool_name,
            "identity": str(identity),
            "operator_id": str(context.get("operator_id") or ""),
            "surface": str(context.get("surface") or ""),
        },
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    digest = hashlib.sha256(payload).hexdigest()[:32]
    return f"tool_{digest}"


def retry_exhausted_result(
    *,
    tool_name: str,
    side_effect_type: str,
    attempt_count: int,
    reason: str,
) -> dict[str, Any]:
    """Build a stable error without leaking transport exception content."""
    side_effect_status = (
        "not_applicable"
        if side_effect_type in {"none", "read"}
        else "may_have_occurred"
    )
    return {
        "ok": False,
        "error": f"Tool '{tool_name}' failed after {attempt_count} retry attempts",
        "error_code": "tool_retry_exhausted",
        "tool_name": tool_name,
        "retryable": True,
        "retry_reason": reason,
        "attempt_count": attempt_count,
        "side_effect_status": side_effect_status,
    }
