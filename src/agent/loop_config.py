"""
Data contracts for the shared agent core loop.

LoopConfig — input parameters for run_agent_loop().
LoopResult — output from run_agent_loop().
"""

from __future__ import annotations

import sqlite3
import time
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Callable, Literal

from src.agent.round_context import RoundContextProvider
from src.agent.tool_receipts import ToolExecutionReceipt
from src.observability.progress import ProgressCallback
from src.observability.contracts import TraceContext
from src.models.context_frame import ContextFrame
from src.memory.exposure_context import ExposureScope

InferenceSurface = Literal["chat", "web", "tool"]


@dataclass
class LoopConfig:
    """Configuration for a single run_agent_loop() invocation."""

    caller_name: str
    tools: list[dict[str, Any]]
    intent: str
    messages: list[dict[str, Any]]
    surface: InferenceSurface = "chat"
    context_frame: ContextFrame | None = None
    exposure_scopes: tuple[ExposureScope, ...] = ()
    max_rounds: int = 8
    temperature: float = 0.3
    effective_project_id: str | None = None
    # True when project_id came from the current user message (force tool inject)
    project_scope_forced: bool = False
    project_scope_typo_corrected: bool = False
    conn: sqlite3.Connection | None = None
    # Optional short-lived writer provider for loops whose execution connection
    # is read-only (for example API request scoped reads).
    write_connection_factory: Callable[[], Any] | None = None
    enable_compression: bool = True
    enable_fabrication_guard: bool = True
    pre_round_hook: Callable[[], str | None] | None = None
    round_context_provider: RoundContextProvider | None = None
    refresh_anchors_each_round: bool = True
    loop_start_utc: datetime | None = None
    blocked_tool_names: frozenset[str] = field(default_factory=frozenset)
    # Runtime authority for model-driven tool calls. When omitted, the loop
    # derives this from the tool schemas supplied to the model.
    runtime_allowed_tool_names: frozenset[str] | None = None
    show_thinking_window: bool = False
    on_stream_delta: Callable[[str | None], None] | None = None
    progress_cb: ProgressCallback | None = None
    # Durable control-plane hook invoked at safe loop boundaries. The payload
    # contains bounded runtime state and must never affect loop correctness.
    on_checkpoint: Callable[[dict[str, Any]], None] | None = None
    required_tools_before_reply: frozenset[str] | None = None
    max_tool_nudge_rounds: int = 2
    # Specialists reserve their last round for a tool-free final response.
    reserve_final_response_round: bool = False
    memory_guard_enabled: bool = False
    operator_id: str | None = None
    thread_id: str | None = None
    thread_artifacts_enabled: bool = False
    digest_tool_results: bool = False
    stream_flags: dict[str, bool] = field(default_factory=dict)
    chat_id: int | None = None
    # Trusted request metadata, never model-supplied tool arguments.
    explicit_operator_instruction: bool = False
    deterministic_authorization: bool = False
    operator_request_text: str | None = None
    # Trusted execution policy propagated to every direct, batched, and
    # delegated tool call. Model-supplied arguments never populate this map.
    execution_context: dict[str, Any] = field(default_factory=dict)
    trace_context: TraceContext | None = None
    # Monotonic process-local deadline propagated by parent tool executions.
    deadline_monotonic: float | None = None
    # Retrieval/tool work must stop before this reserve so a tool-free final
    # synthesis can still run within the authoritative parent deadline.
    finalization_reserve_seconds: float = 8.0

    def tool_phase_deadline(self) -> float | None:
        """Return the child deadline that preserves this loop's finalization reserve."""
        if self.deadline_monotonic is None:
            return None
        reserve = max(0.0, float(self.finalization_reserve_seconds))
        return self.deadline_monotonic - reserve

    def remaining_seconds(self) -> float | None:
        if self.deadline_monotonic is None:
            return None
        return self.deadline_monotonic - time.monotonic()

    def __post_init__(self) -> None:
        """Attach trusted frame quality metadata to every tool call."""
        self.execution_context = dict(self.execution_context)
        self.finalization_reserve_seconds = max(
            0.0, float(self.finalization_reserve_seconds),
        )
        if self.context_frame is not None:
            self.execution_context["context_confidence"] = (
                self.context_frame.context_confidence
            )
            self.execution_context["missing_context_flags"] = tuple(
                self.context_frame.missing_flags
            )


@dataclass
class LoopResult:
    """Output from a completed run_agent_loop() call."""

    reply: str
    messages: list[dict[str, Any]] = field(default_factory=list)
    round_count: int = 0
    tool_history: list[tuple[str, ...]] = field(default_factory=list)
    exit_reason: str = "model_stop"
    answer_streamed: bool = False
    execution_receipts: list[ToolExecutionReceipt] = field(default_factory=list)
    guard_results: list[dict[str, Any]] = field(default_factory=list)
