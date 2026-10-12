"""Privacy-safe transport retry support for Policy-v4 automated reviews."""

from __future__ import annotations

import re
import time
from dataclasses import dataclass
from typing import Any, Callable

StructuredCaller = Callable[[str, dict[str, Any], dict[str, Any]], tuple[dict[str, Any], str]]

_TRANSPORT_ATTEMPTS = 4
_TRANSPORT_BACKOFF_SECONDS = (2.0, 5.0, 10.0)
_TRANSPORT_ERROR_NAMES = frozenset({
    "APIConnectionError",
    "APITimeoutError",
    "BadGatewayError",
    "ConnectError",
    "ConnectTimeout",
    "ConnectionError",
    "ConnectionResetError",
    "GatewayTimeoutError",
    "InternalServerError",
    "LLMTimeoutError",
    "NetworkError",
    "PoolTimeout",
    "RateLimitError",
    "ReadError",
    "ReadTimeout",
    "RemoteProtocolError",
    "ServiceUnavailableError",
    "TimeoutError",
    "WriteError",
    "WriteTimeout",
})
_PARSE_ERROR_NAMES = frozenset({"JSONDecodeError", "LLMParseFailure", "ValidationError"})
_SAFE_NAME_RE = re.compile(r"[^A-Za-z0-9_]")


@dataclass
class TransportRetryState:
    """Count transport retries without counting them as content attempts."""

    retry_count: int = 0


class ReviewStageFailure(RuntimeError):
    """Content, parse, or validation failure with a plaintext-free code."""

    def __init__(self, error_code: str) -> None:
        self.error_code = error_code
        super().__init__(error_code)


class TransportExhaustedError(RuntimeError):
    """Signal that one LLM stage exhausted its isolated transport budget."""

    def __init__(self, error_code: str, retry_count: int) -> None:
        self.error_code = error_code
        self.retry_count = retry_count
        super().__init__(error_code)


def safe_exception_name(exc: BaseException) -> str:
    """Return only a sanitized exception class name, never its message."""
    name = _SAFE_NAME_RE.sub("", type(exc).__name__)
    return name or "Exception"


def _status_code(exc: BaseException) -> int | None:
    direct = getattr(exc, "status_code", None)
    response = getattr(exc, "response", None)
    nested = getattr(response, "status_code", None)
    for value in (direct, nested):
        try:
            if value is not None:
                return int(value)
        except (TypeError, ValueError):
            continue
    return None


def _exception_chain(exc: BaseException) -> list[BaseException]:
    chain: list[BaseException] = []
    seen: set[int] = set()
    current: BaseException | None = exc
    while current is not None and id(current) not in seen:
        chain.append(current)
        seen.add(id(current))
        current = current.__cause__ or current.__context__
    return chain


def transport_exception_name(exc: BaseException) -> str | None:
    """Classify retryable proxy/network failures without reading error messages."""
    for current in _exception_chain(exc):
        name = safe_exception_name(current)
        status = _status_code(current)
        if (
            isinstance(current, (ConnectionError, TimeoutError))
            or name in _TRANSPORT_ERROR_NAMES
            or status == 429
            or (status is not None and 500 <= status <= 599)
        ):
            return name
    return None


def stage_failure(stage: str, category: str, exc: BaseException) -> ReviewStageFailure:
    return ReviewStageFailure(f"{stage}_{category}.{safe_exception_name(exc)}")


def call_stage(
    caller: StructuredCaller,
    state: TransportRetryState,
    stage: str,
    prompt: str,
    payload: dict[str, Any],
    schema: dict[str, Any],
) -> tuple[dict[str, Any], str]:
    """Call one LLM stage with four transport attempts and safe diagnostics."""
    for transport_attempt in range(_TRANSPORT_ATTEMPTS):
        try:
            return caller(prompt, payload, schema)
        except (ReviewStageFailure, TransportExhaustedError):
            raise
        except Exception as exc:
            transport_name = transport_exception_name(exc)
            if transport_name is None:
                category = "parse" if safe_exception_name(exc) in _PARSE_ERROR_NAMES else "call"
                raise stage_failure(stage, category, exc) from exc
            if transport_attempt + 1 >= _TRANSPORT_ATTEMPTS:
                raise TransportExhaustedError(
                    f"{stage}_transport.{transport_name}", state.retry_count
                ) from exc
            state.retry_count += 1
            time.sleep(_TRANSPORT_BACKOFF_SECONDS[transport_attempt])
    raise AssertionError("unreachable transport retry state")


__all__ = [
    "ReviewStageFailure",
    "TransportExhaustedError",
    "TransportRetryState",
    "call_stage",
    "safe_exception_name",
    "stage_failure",
    "transport_exception_name",
]
