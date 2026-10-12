"""
LLM provider status tracker — passive monitoring from actual call outcomes.

No probing: status is updated by llm_call / llm_call_stream / llm_call_with_tools
after each success or failure. The dashboard reads the latest snapshot.

Circuit breaker: after CIRCUIT_BREAKER_THRESHOLD consecutive failures, skip the
provider for an adaptive cooldown (exponential backoff: 60→120→240→300 cap).
After cooldown, one retry is allowed — if it fails, circuit re-opens immediately
with doubled cooldown.
"""

import time
from dataclasses import dataclass, field
from threading import Lock


CIRCUIT_BREAKER_THRESHOLD = 3
CIRCUIT_BREAKER_BASE_COOLDOWN_SEC = 60
CIRCUIT_BREAKER_MAX_COOLDOWN_SEC = 300


@dataclass
class ProviderSnapshot:
    reachable: bool = False
    last_check: float = 0.0
    last_latency_ms: int | None = None
    last_error: str | None = None
    consecutive_failures: int = 0
    circuit_open_since: float = 0.0
    current_cooldown_sec: float = CIRCUIT_BREAKER_BASE_COOLDOWN_SEC
    total_trips: int = 0


_lock = Lock()
_status: dict[str, ProviderSnapshot] = {
    "primary": ProviderSnapshot(),
    "backup": ProviderSnapshot(),
}


def record_success(provider: str, latency_ms: int) -> None:
    with _lock:
        snap = _status.get(provider)
        if snap is None:
            return
        snap.reachable = True
        snap.last_check = time.time()
        snap.last_latency_ms = latency_ms
        snap.last_error = None
        snap.consecutive_failures = 0
        snap.circuit_open_since = 0.0
        snap.current_cooldown_sec = CIRCUIT_BREAKER_BASE_COOLDOWN_SEC
        snap.total_trips = 0


def record_failure(provider: str, error: str) -> None:
    with _lock:
        snap = _status.get(provider)
        if snap is None:
            return
        snap.reachable = False
        snap.last_check = time.time()
        snap.last_latency_ms = None
        snap.last_error = error[:200]
        snap.consecutive_failures += 1
        if snap.consecutive_failures >= CIRCUIT_BREAKER_THRESHOLD and snap.circuit_open_since == 0.0:
            snap.circuit_open_since = time.time()
            snap.total_trips += 1
            snap.current_cooldown_sec = min(
                CIRCUIT_BREAKER_BASE_COOLDOWN_SEC * (2 ** (snap.total_trips - 1)),
                CIRCUIT_BREAKER_MAX_COOLDOWN_SEC,
            )


def is_circuit_open(provider: str) -> bool:
    """Return True if the circuit breaker is tripped (skip this provider)."""
    with _lock:
        snap = _status.get(provider)
        if snap is None:
            return False
        if snap.circuit_open_since == 0.0:
            return False
        elapsed = time.time() - snap.circuit_open_since
        if elapsed >= snap.current_cooldown_sec:
            snap.circuit_open_since = 0.0
            snap.consecutive_failures = CIRCUIT_BREAKER_THRESHOLD - 1
            return False
        return True


def get_status() -> dict[str, ProviderSnapshot]:
    with _lock:
        return {k: ProviderSnapshot(
            reachable=v.reachable,
            last_check=v.last_check,
            last_latency_ms=v.last_latency_ms,
            last_error=v.last_error,
            consecutive_failures=v.consecutive_failures,
            circuit_open_since=v.circuit_open_since,
            current_cooldown_sec=v.current_cooldown_sec,
            total_trips=v.total_trips,
        ) for k, v in _status.items()}


def reset() -> None:
    """Reset all status — for tests."""
    with _lock:
        for snap in _status.values():
            snap.reachable = False
            snap.last_check = 0.0
            snap.last_latency_ms = None
            snap.last_error = None
            snap.consecutive_failures = 0
            snap.circuit_open_since = 0.0
            snap.current_cooldown_sec = CIRCUIT_BREAKER_BASE_COOLDOWN_SEC
            snap.total_trips = 0
