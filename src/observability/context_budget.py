"""Per-request context token budget tracking for Host + Specialist loops."""

from __future__ import annotations

import logging
from contextvars import ContextVar
from dataclasses import dataclass

logger = logging.getLogger(__name__)


@dataclass
class ContextBudgetState:
    host: int = 0
    specialist: int = 0

    @property
    def total(self) -> int:
        return self.host + self.specialist


_budget_var: ContextVar[ContextBudgetState | None] = ContextVar("context_budget", default=None)
_last_specialist: ContextVar[str | None] = ContextVar("context_budget_specialist", default=None)


def reset_context_budget() -> None:
    """Start a fresh budget accumulator (call at Host entry)."""
    _budget_var.set(ContextBudgetState())
    _last_specialist.set(None)


def get_context_budget() -> ContextBudgetState:
    state = _budget_var.get()
    if state is None:
        state = ContextBudgetState()
        _budget_var.set(state)
    return state


def record_loop_budget(
    caller_name: str,
    messages: list[dict],
    tools: list[dict] | None,
    *,
    estimate_fn,
) -> int:
    """Record estimated input tokens for one ``run_agent_loop`` invocation."""
    tokens = estimate_fn(messages, tools)
    state = get_context_budget()
    if caller_name.startswith("specialist:"):
        state.specialist += tokens
        _last_specialist.set(caller_name.removeprefix("specialist:"))
    else:
        state.host += tokens
    logger.info(
        "context_budget={host:%d, specialist:%d, total:%d} caller=%s est=%d",
        state.host,
        state.specialist,
        state.total,
        caller_name,
        tokens,
    )
    return tokens


def trace_metadata() -> dict | None:
    """Metadata blob for ``llm_traces.trace_metadata`` (best-effort)."""
    state = _budget_var.get()
    if state is None or state.total == 0:
        return None
    meta: dict = {
        "host_tokens": state.host,
        "specialist_tokens": state.specialist,
        "total_tokens": state.total,
    }
    specialist = _last_specialist.get()
    if specialist:
        meta["specialist"] = specialist
    try:
        from config import defaults

        if getattr(defaults, "ORCHESTRATOR_MODE", False):
            meta["orchestrator"] = True
    except Exception:
        pass
    return meta