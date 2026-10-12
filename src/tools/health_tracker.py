"""In-memory tool runtime health and selection-quality signals.

The tracker is deliberately process-local and dependency-free. It distinguishes
runtime/infrastructure failures from authorization, input, and business-domain
outcomes so circuit protection cannot be tripped by ordinary model mistakes or
legitimate tool rejections.
"""

from __future__ import annotations

import time
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from threading import RLock
from typing import Any, Callable

CIRCUIT_BREAKER_THRESHOLD = 3
CIRCUIT_BREAKER_BASE_COOLDOWN_SEC = 30.0
CIRCUIT_BREAKER_MAX_COOLDOWN_SEC = 300.0
CIRCUIT_PROTECTED_SIDE_EFFECTS = frozenset({"none", "read", "external"})
MAX_TRACKED_TOOL_STATES = 256
TOOL_STATE_OVERFLOW_NAME = "__tool_state_overflow__"


@dataclass(frozen=True)
class ToolExecutionDecision:
    """Atomic pre-execution circuit decision."""

    allowed: bool
    state: str
    reason: str
    retry_after_seconds: float = 0.0
    circuit_eligible: bool = False

    def blocked_result(self, tool_name: str) -> dict[str, Any]:
        return {
            "ok": False,
            "error": (
                f"Tool '{tool_name}' is temporarily unavailable after repeated "
                "runtime failures"
            ),
            "error_code": "tool_circuit_open",
            "tool_name": tool_name,
            "retry_after_seconds": round(max(0.0, self.retry_after_seconds), 3),
            "health_state": self.state,
            "side_effect_status": "not_started",
        }


@dataclass
class _ToolState:
    side_effect_type: str = "unknown"
    exposure_count: int = 0
    policy_required_count: int = 0
    selection_count: int = 0
    authorization_allowed_count: int = 0
    selection_blocked_count: int = 0
    duplicate_selection_count: int = 0
    execution_count: int = 0
    model_execution_count: int = 0
    runtime_success_count: int = 0
    infrastructure_failure_count: int = 0
    receipt_success_count: int = 0
    receipt_failure_count: int = 0
    receipt_denied_count: int = 0
    circuit_blocked_count: int = 0
    consecutive_infrastructure_failures: int = 0
    total_circuit_trips: int = 0
    current_cooldown_seconds: float = CIRCUIT_BREAKER_BASE_COOLDOWN_SEC
    circuit_open_until_monotonic: float = 0.0
    circuit_open_until_epoch: float = 0.0
    probe_in_flight: bool = False
    last_latency_ms: float | None = None
    last_runtime_error_code: str | None = None
    last_runtime_error: str | None = None
    last_outcome_error_code: str | None = None
    last_execution_at_epoch: float = 0.0
    last_selection_block_reason: str | None = None


class ToolHealthTracker:
    """Thread-safe, bounded health tracker for the registered tool plane."""

    def __init__(
        self,
        *,
        failure_threshold: int = CIRCUIT_BREAKER_THRESHOLD,
        base_cooldown_seconds: float = CIRCUIT_BREAKER_BASE_COOLDOWN_SEC,
        max_cooldown_seconds: float = CIRCUIT_BREAKER_MAX_COOLDOWN_SEC,
        max_tracked_tools: int = MAX_TRACKED_TOOL_STATES,
        monotonic_clock: Callable[[], float] = time.monotonic,
        wall_clock: Callable[[], float] = time.time,
    ) -> None:
        self.failure_threshold = max(1, int(failure_threshold))
        self.base_cooldown_seconds = max(0.001, float(base_cooldown_seconds))
        self.max_cooldown_seconds = max(
            self.base_cooldown_seconds,
            float(max_cooldown_seconds),
        )
        self.max_tracked_tools = max(1, int(max_tracked_tools))
        self._monotonic = monotonic_clock
        self._wall = wall_clock
        self._lock = RLock()
        self._states: dict[str, _ToolState] = {}

    def _state(self, tool_name: str, side_effect_type: str | None = None) -> _ToolState:
        key = str(tool_name)
        if (
            key != TOOL_STATE_OVERFLOW_NAME
            and key not in self._states
            and len(self._states) >= self.max_tracked_tools - 1
        ):
            key = TOOL_STATE_OVERFLOW_NAME
        state = self._states.setdefault(
            key,
            _ToolState(current_cooldown_seconds=self.base_cooldown_seconds),
        )
        if side_effect_type and key != TOOL_STATE_OVERFLOW_NAME:
            state.side_effect_type = str(side_effect_type)
        return state

    @staticmethod
    def _circuit_eligible(side_effect_type: str) -> bool:
        return side_effect_type in CIRCUIT_PROTECTED_SIDE_EFFECTS

    def record_exposure(self, tool_names: Iterable[str]) -> None:
        with self._lock:
            for name in tool_names:
                if name:
                    self._state(str(name)).exposure_count += 1

    def record_required(self, tool_names: Iterable[str]) -> None:
        with self._lock:
            for name in tool_names:
                if name:
                    self._state(str(name)).policy_required_count += 1

    def record_selection(self, tool_name: str, *, repeated: bool = False) -> None:
        with self._lock:
            state = self._state(tool_name)
            state.selection_count += 1
            if repeated:
                state.duplicate_selection_count += 1

    def record_authorization(
        self,
        tool_name: str,
        *,
        allowed: bool,
        reason: str | None = None,
    ) -> None:
        with self._lock:
            state = self._state(tool_name)
            if allowed:
                state.authorization_allowed_count += 1
            else:
                state.selection_blocked_count += 1
                state.last_selection_block_reason = (reason or "blocked")[:120]

    def record_pre_execution_block(self, tool_name: str, *, reason: str) -> None:
        with self._lock:
            state = self._state(tool_name)
            state.selection_blocked_count += 1
            state.last_selection_block_reason = reason[:120]

    def begin_execution(
        self,
        tool_name: str,
        *,
        side_effect_type: str,
        selection_source: str | None = None,
    ) -> ToolExecutionDecision:
        """Reserve an execution slot or fail closed while a circuit is open."""
        now = self._monotonic()
        with self._lock:
            state = self._state(tool_name, side_effect_type)
            eligible = self._circuit_eligible(state.side_effect_type)
            if eligible and state.probe_in_flight:
                state.circuit_blocked_count += 1
                return ToolExecutionDecision(
                    allowed=False,
                    state="half_open",
                    reason="half_open_probe_in_flight",
                    circuit_eligible=True,
                )
            if eligible and state.circuit_open_until_monotonic > now:
                state.circuit_blocked_count += 1
                return ToolExecutionDecision(
                    allowed=False,
                    state="open",
                    reason="circuit_cooldown",
                    retry_after_seconds=state.circuit_open_until_monotonic - now,
                    circuit_eligible=True,
                )
            if eligible and state.circuit_open_until_monotonic > 0.0:
                state.circuit_open_until_monotonic = 0.0
                state.circuit_open_until_epoch = 0.0
                state.probe_in_flight = True
                decision_state = "half_open"
            else:
                decision_state = "closed"

            state.execution_count += 1
            if selection_source == "model":
                state.model_execution_count += 1
            state.last_execution_at_epoch = self._wall()
            return ToolExecutionDecision(
                allowed=True,
                state=decision_state,
                reason="execution_allowed",
                circuit_eligible=eligible,
            )

    def record_runtime_success(self, tool_name: str, *, latency_ms: float) -> None:
        with self._lock:
            state = self._state(tool_name)
            state.runtime_success_count += 1
            state.consecutive_infrastructure_failures = 0
            state.probe_in_flight = False
            state.circuit_open_until_monotonic = 0.0
            state.circuit_open_until_epoch = 0.0
            state.current_cooldown_seconds = self.base_cooldown_seconds
            state.last_latency_ms = round(max(0.0, float(latency_ms)), 3)
            state.last_runtime_error_code = None
            state.last_runtime_error = None
            state.last_execution_at_epoch = self._wall()

    def record_runtime_failure(
        self,
        tool_name: str,
        *,
        side_effect_type: str,
        error_code: str,
        error: str,
        latency_ms: float,
    ) -> None:
        now_monotonic = self._monotonic()
        now_epoch = self._wall()
        with self._lock:
            state = self._state(tool_name, side_effect_type)
            was_probe = state.probe_in_flight
            state.probe_in_flight = False
            state.infrastructure_failure_count += 1
            state.consecutive_infrastructure_failures += 1
            state.last_latency_ms = round(max(0.0, float(latency_ms)), 3)
            state.last_runtime_error_code = str(error_code)[:120]
            state.last_runtime_error = str(error)[:200]
            state.last_execution_at_epoch = now_epoch

            eligible = self._circuit_eligible(state.side_effect_type)
            already_open = state.circuit_open_until_monotonic > now_monotonic
            should_trip = eligible and not already_open and (
                was_probe
                or state.consecutive_infrastructure_failures >= self.failure_threshold
            )
            if should_trip:
                cooldown = min(
                    state.current_cooldown_seconds,
                    self.max_cooldown_seconds,
                )
                state.circuit_open_until_monotonic = now_monotonic + cooldown
                state.circuit_open_until_epoch = now_epoch + cooldown
                state.total_circuit_trips += 1
                state.current_cooldown_seconds = min(
                    cooldown * 2,
                    self.max_cooldown_seconds,
                )

    def record_outcome(
        self,
        tool_name: str,
        *,
        status: str,
        error_code: str | None = None,
    ) -> None:
        """Record the authoritative receipt outcome, separate from runtime health."""
        with self._lock:
            state = self._state(tool_name)
            if status == "succeeded":
                state.receipt_success_count += 1
            elif status == "denied":
                state.receipt_denied_count += 1
            else:
                state.receipt_failure_count += 1
            if error_code and status != "succeeded":
                state.last_outcome_error_code = str(error_code)[:120]

    def should_block(self, tool_name: str) -> bool:
        """Return current circuit state without reserving a half-open probe."""
        now = self._monotonic()
        with self._lock:
            state = self._states.get(tool_name)
            if state is None or not self._circuit_eligible(state.side_effect_type):
                return False
            return state.circuit_open_until_monotonic > now or state.probe_in_flight

    @staticmethod
    def _rate(numerator: int, denominator: int) -> float | None:
        return round(numerator / denominator, 4) if denominator else None

    @staticmethod
    def _timestamp(epoch: float) -> str | None:
        if epoch <= 0:
            return None
        return datetime.fromtimestamp(epoch, tz=timezone.utc).isoformat()

    def snapshot(
        self,
        contracts: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        now_monotonic = self._monotonic()
        now_epoch = self._wall()
        with self._lock:
            names = set(self._states)
            if contracts is not None:
                names.update(str(name) for name in contracts)
            tools = []
            for name in sorted(names):
                contract = contracts.get(name) if contracts is not None else None
                contract_effect = getattr(contract, "side_effect_type", None)
                state = self._states.get(name) or _ToolState(
                    side_effect_type=str(contract_effect or "unknown"),
                    current_cooldown_seconds=self.base_cooldown_seconds,
                )
                side_effect_type = str(contract_effect or state.side_effect_type)
                retry_after = max(
                    0.0,
                    state.circuit_open_until_monotonic - now_monotonic,
                )
                if retry_after > 0:
                    health_state = "open"
                elif state.probe_in_flight or state.circuit_open_until_monotonic > 0:
                    health_state = "half_open"
                elif state.consecutive_infrastructure_failures:
                    health_state = "degraded"
                elif state.execution_count:
                    health_state = "healthy"
                else:
                    health_state = "idle"
                receipt_total = (
                    state.receipt_success_count
                    + state.receipt_failure_count
                    + state.receipt_denied_count
                )
                tools.append({
                    "tool_name": name,
                    "side_effect_type": side_effect_type,
                    "state": health_state,
                    "circuit_eligible": self._circuit_eligible(side_effect_type),
                    "exposure_count": state.exposure_count,
                    "policy_required_count": state.policy_required_count,
                    "selection_count": state.selection_count,
                    "authorization_allowed_count": state.authorization_allowed_count,
                    "selection_blocked_count": state.selection_blocked_count,
                    "duplicate_selection_count": state.duplicate_selection_count,
                    "execution_count": state.execution_count,
                    "model_execution_count": state.model_execution_count,
                    "runtime_success_count": state.runtime_success_count,
                    "infrastructure_failure_count": state.infrastructure_failure_count,
                    "receipt_success_count": state.receipt_success_count,
                    "receipt_failure_count": state.receipt_failure_count,
                    "receipt_denied_count": state.receipt_denied_count,
                    "circuit_blocked_count": state.circuit_blocked_count,
                    "consecutive_infrastructure_failures": (
                        state.consecutive_infrastructure_failures
                    ),
                    "total_circuit_trips": state.total_circuit_trips,
                    "last_latency_ms": state.last_latency_ms,
                    "last_runtime_error_code": state.last_runtime_error_code,
                    "last_runtime_error": state.last_runtime_error,
                    "last_outcome_error_code": state.last_outcome_error_code,
                    "last_selection_block_reason": state.last_selection_block_reason,
                    "last_execution_at": self._timestamp(state.last_execution_at_epoch),
                    "circuit_open_until": self._timestamp(
                        state.circuit_open_until_epoch
                    ),
                    "retry_after_seconds": round(retry_after, 3),
                    "selection_rate": self._rate(
                        state.selection_count,
                        state.exposure_count,
                    ),
                    "authorization_rate": self._rate(
                        state.authorization_allowed_count,
                        state.selection_count,
                    ),
                    "duplicate_selection_rate": self._rate(
                        state.duplicate_selection_count,
                        state.selection_count,
                    ),
                    "runtime_success_rate": self._rate(
                        state.runtime_success_count,
                        state.execution_count,
                    ),
                    "outcome_success_rate": self._rate(
                        state.receipt_success_count,
                        receipt_total,
                    ),
                })

            totals = {
                key: sum(int(tool[key]) for tool in tools)
                for key in (
                    "exposure_count",
                    "policy_required_count",
                    "selection_count",
                    "authorization_allowed_count",
                    "selection_blocked_count",
                    "duplicate_selection_count",
                    "execution_count",
                    "model_execution_count",
                    "runtime_success_count",
                    "infrastructure_failure_count",
                    "receipt_success_count",
                    "receipt_failure_count",
                    "receipt_denied_count",
                    "circuit_blocked_count",
                )
            }
            open_count = sum(tool["state"] == "open" for tool in tools)
            half_open_count = sum(tool["state"] == "half_open" for tool in tools)
            degraded_count = sum(tool["state"] == "degraded" for tool in tools)
            receipt_total = (
                totals["receipt_success_count"]
                + totals["receipt_failure_count"]
                + totals["receipt_denied_count"]
            )
            summary = {
                "status": (
                    "degraded"
                    if open_count or half_open_count or degraded_count
                    else "ok"
                ),
                "tracked_tool_count": len(tools),
                "open_circuit_count": open_count,
                "half_open_circuit_count": half_open_count,
                "degraded_tool_count": degraded_count,
                **totals,
                "authorization_rate": self._rate(
                    totals["authorization_allowed_count"],
                    totals["selection_count"],
                ),
                "duplicate_selection_rate": self._rate(
                    totals["duplicate_selection_count"],
                    totals["selection_count"],
                ),
                "model_selection_execution_rate": self._rate(
                    totals["model_execution_count"],
                    totals["selection_count"],
                ),
                "runtime_success_rate": self._rate(
                    totals["runtime_success_count"],
                    totals["execution_count"],
                ),
                "outcome_success_rate": self._rate(
                    totals["receipt_success_count"],
                    receipt_total,
                ),
            }
            return {
                "generated_at": self._timestamp(now_epoch),
                "summary": summary,
                "tools": tools,
            }

    def reset(self) -> None:
        with self._lock:
            self._states.clear()


_tracker = ToolHealthTracker()


def get_tracker() -> ToolHealthTracker:
    return _tracker


def get_status(contracts: Mapping[str, Any] | None = None) -> dict[str, Any]:
    return _tracker.snapshot(contracts)


def reset() -> None:
    _tracker.reset()
