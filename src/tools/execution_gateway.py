"""Central execution boundary for registered agent tools."""

from __future__ import annotations

import sqlite3
import threading
import time
from collections.abc import Callable, Mapping
from contextlib import nullcontext
from typing import Any


from src.governance.tool_authorization import ToolExecutionContext, authorize_tool_call
from src.tools.contract_validation import (
    ToolContractValidationError,
    json_compatible as _json_compatible,
    schema_errors as _schema_errors,
)
from src.tools.context_confidence import (
    ContextConfidenceError,
    evaluate_context_confidence,
)
from src.tools.health_tracker import ToolHealthTracker
from src.tools.contracts import ToolContract
from src.tools.registry import TOOL_BY_NAME
from src.tools.retry_policy import (
    retry_delay,
    retry_exhausted_result,
    retry_plan,
    retry_reason,
    trusted_idempotency_key,
)

ToolHandler = Callable[[sqlite3.Connection, dict[str, Any]], Any]
HandlerRegistry = Mapping[str, ToolHandler] | Callable[[], Mapping[str, ToolHandler]]
ContractRegistry = Mapping[str, ToolContract] | Callable[[], Mapping[str, ToolContract]]
ContextBinder = Callable[..., Any]

_READ_LIKE_EFFECTS = frozenset({"none", "read"})
_ASSISTANT_CONTEXT_ARGUMENTS = (
    ("assistant_scope_mode", "_assistant_scope_mode"),
    ("assistant_scope_project_ids", "_assistant_scope_project_ids"),
    ("assistant_source_sink", "_assistant_source_sink"),
    ("assistant_tool_result_sink", "_assistant_tool_result_sink"),
    ("assistant_web_enabled", "_assistant_web_enabled"),
    ("assistant_as_of", "_assistant_as_of"),
    ("assistant_all_projects_requested", "_assistant_all_projects_requested"),
)


def _tool_name_set(value: Any) -> frozenset[str] | None:
    if value is None:
        return None
    if isinstance(value, str):
        return frozenset({value})
    try:
        return frozenset(str(item) for item in value)
    except TypeError:
        return frozenset({str(value)})



class ToolExecutionGateway:
    """Resolve, authorize, validate, and invoke every registered tool handler.

    Connection routing and batching remain executor concerns. This class
    owns authorization, schema validation, deadline enforcement, and the
    security-sensitive boundary before a handler receives trusted arguments.
    """

    def __init__(
        self,
        handlers: HandlerRegistry,
        contracts: ContractRegistry | None = None,
        *,
        health_tracker: ToolHealthTracker | None = None,
    ) -> None:
        self._handlers = handlers
        self._contracts = contracts or TOOL_BY_NAME
        self._health_tracker = health_tracker

    def _handler_registry(self) -> Mapping[str, ToolHandler]:
        return self._handlers() if callable(self._handlers) else self._handlers

    def _contract_registry(self) -> Mapping[str, ToolContract]:
        return self._contracts() if callable(self._contracts) else self._contracts

    def resolve(self, tool_name: str) -> tuple[ToolContract, ToolHandler]:
        contract = self._contract_registry().get(tool_name)
        if contract is None:
            raise ValueError(f"Unknown tool: {tool_name}")
        handler = self._handler_registry().get(tool_name)
        if handler is None:
            raise RuntimeError(f"Registered tool has no executor handler: {tool_name}")
        return contract, handler

    def needs_write(self, tool_name: str) -> bool:
        contract = self._contract_registry().get(tool_name)
        return contract is not None and contract.side_effect_type not in _READ_LIKE_EFFECTS

    def authorize(
        self,
        contract: ToolContract,
        execution_context: Mapping[str, Any],
        *,
        task_type: str,
    ) -> None:
        if (
            task_type != "*"
            and "*" not in contract.allowed_task_types
            and task_type not in contract.allowed_task_types
        ):
            raise PermissionError(
                f"Tool '{contract.name}' is not allowed for task type '{task_type}'"
            )

        tool_context = execution_context.get("tool_context")
        if (
            tool_context is not None
            and "*" not in contract.allowed_contexts
            and str(tool_context) not in contract.allowed_contexts
        ):
            raise PermissionError(
                f"Tool '{contract.name}' is not allowed in tool context '{tool_context}'"
            )

        allowed_names = _tool_name_set(
            execution_context.get("runtime_allowed_tool_names")
        )
        if allowed_names is not None and contract.name not in allowed_names:
            raise PermissionError(
                f"Tool '{contract.name}' is not allowed by the runtime tool policy"
            )

        from src.email.analysis_execution import AnalysisWritePolicy
        analysis_policy = execution_context.get("inbound_analysis_policy")
        if isinstance(analysis_policy, AnalysisWritePolicy):
            analysis_policy.authorize_tool(contract.name, contract.side_effect_type)

        blocked_names = _tool_name_set(execution_context.get("blocked_tool_names"))
        if blocked_names and contract.name in blocked_names:
            raise PermissionError(
                f"Tool '{contract.name}' is blocked by the runtime tool policy"
            )

        if (
            execution_context.get("read_only_execution")
            or execution_context.get("side_effects_forbidden")
        ) and contract.side_effect_type not in _READ_LIKE_EFFECTS:
            raise PermissionError(
                f"Tool '{contract.name}' is blocked by the read-only execution policy"
            )

        authorization_error = authorize_tool_call(
            contract,
            ToolExecutionContext(
                operator_id=(
                    str(execution_context["operator_id"])
                    if execution_context.get("operator_id") is not None
                    else None
                ),
                surface=str(execution_context.get("surface") or "internal"),
                role=(
                    str(execution_context["role"])
                    if execution_context.get("role") is not None
                    else None
                ),
                specialist_type=(
                    str(execution_context["specialist_type"])
                    if execution_context.get("specialist_type") is not None
                    else None
                ),
                approved_action_id=(
                    str(execution_context["approved_action_id"])
                    if execution_context.get("approved_action_id") is not None
                    else None
                ),
            ),
        )
        if authorization_error:
            raise PermissionError(authorization_error)

        confidence_decision = evaluate_context_confidence(
            minimum=contract.minimum_context_confidence,
            execution_context=execution_context,
        )
        if not confidence_decision.allowed:
            raise ContextConfidenceError(contract.name, confidence_decision)

    def validate_input(
        self,
        contract: ToolContract,
        arguments: Mapping[str, Any],
    ) -> None:
        errors = _schema_errors(contract.input_schema, _json_compatible(arguments))
        if errors:
            raise ToolContractValidationError(contract.name, errors)

    def validate_output(self, contract: ToolContract, result: Any) -> dict[str, Any] | None:
        errors = _schema_errors(contract.output_schema, _json_compatible(result))
        if not errors:
            return None
        side_effect_status = (
            "may_have_occurred"
            if contract.side_effect_type not in _READ_LIKE_EFFECTS
            else "not_applicable"
        )
        return {
            "ok": False,
            "error": f"Tool '{contract.name}' output schema validation failed",
            "error_code": "tool_output_schema_invalid",
            "tool_name": contract.name,
            "schema_errors": errors,
            "side_effect_status": side_effect_status,
        }

    @staticmethod
    def _deadline_result(
        contract: ToolContract,
        *,
        side_effect_status: str,
        effective_timeout_seconds: float,
    ) -> dict[str, Any]:
        return {
            "ok": False,
            "error": f"Tool '{contract.name}' exceeded its execution deadline",
            "error_code": "tool_timeout",
            "tool_name": contract.name,
            "timeout_seconds": round(max(0.0, effective_timeout_seconds), 6),
            "contract_timeout_seconds": contract.timeout_seconds,
            "side_effect_status": side_effect_status,
        }

    @staticmethod
    def _salvage_late_read_result(
        contract: ToolContract,
        result: Any,
        *,
        effective_timeout_seconds: float,
    ) -> dict[str, Any] | None:
        """Preserve a structured read result that completed just after its deadline."""
        normalized = _json_compatible(result)
        if not isinstance(normalized, Mapping):
            return None
        salvaged = dict(normalized)
        if (
            salvaged.get("ok") is False
            or salvaged.get("success") is False
            or salvaged.get("error")
        ):
            return None
        salvaged["ok"] = True
        salvaged["completion_status"] = "partial"
        salvaged["outcome"] = "partial"
        salvaged["late_result_salvaged"] = True
        salvaged["deadline_exceeded"] = True
        salvaged["error_code"] = "tool_timeout"
        salvaged["timeout_seconds"] = round(
            max(0.0, effective_timeout_seconds), 6,
        )
        salvaged["contract_timeout_seconds"] = contract.timeout_seconds
        salvaged["side_effect_status"] = "not_applicable"
        return salvaged

    @staticmethod
    def _effective_deadline(
        contract: ToolContract,
        context: Mapping[str, Any],
    ) -> float:
        own_deadline = time.monotonic() + max(0.0, float(contract.timeout_seconds))
        parent_value = context.get("deadline_monotonic")
        try:
            parent_deadline = float(parent_value)
        except (TypeError, ValueError):
            return own_deadline
        return min(own_deadline, parent_deadline)

    def execute(
        self,
        conn: sqlite3.Connection,
        *,
        task_type: str,
        tool_name: str,
        arguments: Mapping[str, Any],
        execution_context: Mapping[str, Any] | None = None,
        context_binder: ContextBinder | None = None,
    ) -> Any:
        contract, handler = self.resolve(tool_name)
        context = dict(execution_context or {})
        # Authorization intentionally precedes model argument validation so a
        # denied tool never reveals contract details or reaches a handler.
        self.authorize(contract, context, task_type=task_type)
        if not isinstance(arguments, Mapping):
            raise ToolContractValidationError(
                tool_name,
                [{
                    "path": "$",
                    "keyword": "type",
                    "message": "Expected type: object",
                }],
            )
        # Validate only model-supplied arguments. Trusted runtime fields are
        # injected after validation and are deliberately absent from schemas.
        self.validate_input(contract, arguments)
        # Project-only calls can still select a typed operation in the legacy
        # default_operator/legacy scope. Require both runtime bindings even when
        # the model supplies no scope fields. A deliberate internal compatibility
        # call may opt into that old scope, but never with model-supplied identity
        # or an operation ID; the opt-in is trusted context, not a tool argument.
        if tool_name in {"confirm_update", "cancel_update"}:
            runtime_bound = (
                bool(str(context.get("operator_id") or "").strip())
                and bool(str(context.get("chat_id") or "").strip())
            )
            model_scope = any(
                key in arguments for key in ("operation_id", "_operator_id", "chat_id")
            )
            authorized_legacy = (
                context.get("legacy_project_update_authorized") is True
                and context.get("surface") == "internal"
                and not str(context.get("operator_id") or "").strip()
                and not str(context.get("chat_id") or "").strip()
                and not model_scope
            )
            if not runtime_bound and not authorized_legacy:
                raise PermissionError(
                    f"Tool '{tool_name}' requires a trusted operator and chat "
                    "or explicit internal legacy authorization"
                )
        from src.email.analysis_execution import AnalysisWritePolicy
        analysis_policy = context.get("inbound_analysis_policy")
        if isinstance(analysis_policy, AnalysisWritePolicy):
            analysis_policy.authorize_arguments(contract.name, arguments)

        deadline_started = time.monotonic()
        deadline = self._effective_deadline(contract, context)
        effective_timeout_seconds = max(0.0, deadline - deadline_started)
        context["deadline_monotonic"] = deadline
        context["tool_timeout_seconds"] = effective_timeout_seconds
        if deadline <= time.monotonic():
            # A consumed parent budget is not evidence that this tool is unhealthy.
            return self._deadline_result(
                contract,
                side_effect_status="not_started",
                effective_timeout_seconds=effective_timeout_seconds,
            )

        health_decision = None
        if self._health_tracker is not None:
            health_decision = self._health_tracker.begin_execution(
                contract.name,
                side_effect_type=contract.side_effect_type,
                selection_source=(
                    str(context["selection_source"])
                    if context.get("selection_source") is not None
                    else None
                ),
            )
            if not health_decision.allowed:
                return health_decision.blocked_result(contract.name)

        execution_started = time.monotonic()

        def record_success() -> None:
            if self._health_tracker is not None:
                self._health_tracker.record_runtime_success(
                    contract.name,
                    latency_ms=(time.monotonic() - execution_started) * 1000,
                )

        def record_failure(error_code: str, error: str) -> None:
            if self._health_tracker is not None:
                self._health_tracker.record_runtime_failure(
                    contract.name,
                    side_effect_type=contract.side_effect_type,
                    error_code=error_code,
                    error=error,
                    latency_ms=(time.monotonic() - execution_started) * 1000,
                )

        trusted_args = dict(arguments)
        if context.get("operator_id") is not None:
            trusted_args["_operator_id"] = str(context["operator_id"])
        if context.get("chat_id") is not None:
            trusted_args["chat_id"] = str(context["chat_id"])
        for context_key, argument_key in _ASSISTANT_CONTEXT_ARGUMENTS:
            if context_key in context:
                trusted_args[argument_key] = context[context_key]
        trusted_args["_trusted_surface"] = str(context.get("surface") or "internal")
        if tool_name in {"confirm_update", "cancel_update"} and context.get("owned_write_scope"):
            # The tool can stage changes; the scoped writer owns commit and effects.
            trusted_args["commit"] = False
            if tool_name == "confirm_update":
                trusted_args["publish_event"] = False
                trusted_args["_post_commit_effects"] = context.get("post_commit_effects")
        if tool_name == "project_reconcile":
            # Never accept callbacks from model arguments; only the caller can own
            # the effects list for a borrowed transaction.
            effects = context.get("post_commit_effects")
            trusted_args["_post_commit_effects"] = effects if isinstance(effects, list) else None
        if tool_name in {"update_project", "update_project_batch", "project_reconcile"}:
            trusted_args["_trusted_explicit_operator"] = bool(
                context.get("explicit_operator_instruction")
                or context.get("deterministic_authorization")
            )
        retry_seed = trusted_idempotency_key(contract.name, context)
        if contract.idempotency_mode == "trusted_key":
            trusted_args["_idempotency_key"] = retry_seed

        plan = retry_plan(contract.retry_policy)
        retry_is_safe = (
            contract.side_effect_type in _READ_LIKE_EFFECTS
            or contract.idempotency_mode == "trusted_key"
        )
        max_attempts = plan.max_attempts if retry_is_safe else 1

        for attempt_count in range(1, max_attempts + 1):
            context["tool_attempt"] = attempt_count
            interrupt_lock = threading.Lock()
            interrupt_active = True
            deadline_fired = False

            def _interrupt_if_active() -> None:
                nonlocal deadline_fired, interrupt_active
                with interrupt_lock:
                    if interrupt_active:
                        deadline_fired = True
                        if isinstance(conn, sqlite3.Connection):
                            conn.interrupt()

            remaining = max(0.0, deadline - time.monotonic())
            if remaining <= 0:
                side_effect_status = (
                    "may_have_occurred"
                    if attempt_count > 1
                    and contract.side_effect_type not in _READ_LIKE_EFFECTS
                    else "not_started"
                )
                result = self._deadline_result(
                    contract,
                    side_effect_status=side_effect_status,
                    effective_timeout_seconds=effective_timeout_seconds,
                )
                record_failure("tool_timeout", str(result["error"]))
                return result
            timer = threading.Timer(remaining, _interrupt_if_active)
            timer.daemon = True
            timer.start()
            try:
                bound_context = (
                    context_binder(**context)
                    if context_binder is not None
                    else nullcontext()
                )
                with bound_context:
                    result = handler(conn, trusted_args)
            except Exception as exc:
                if deadline_fired or time.monotonic() >= deadline:
                    side_effect_status = (
                        "may_have_occurred"
                        if contract.side_effect_type not in _READ_LIKE_EFFECTS
                        else "not_applicable"
                    )
                    result = self._deadline_result(
                        contract,
                        side_effect_status=side_effect_status,
                        effective_timeout_seconds=effective_timeout_seconds,
                    )
                    record_failure("tool_timeout", str(result["error"]))
                    return result
                reason = retry_reason(exc)
                if reason is None or attempt_count >= max_attempts:
                    if reason is not None and max_attempts > 1:
                        from src.agent.control_plane_metrics import (
                            control_plane_metrics,
                        )

                        control_plane_metrics.increment(
                            "tool_retry", outcome="exhausted"
                        )
                        result = retry_exhausted_result(
                            tool_name=contract.name,
                            side_effect_type=contract.side_effect_type,
                            attempt_count=attempt_count,
                            reason=reason,
                        )
                        record_failure(
                            "tool_retry_exhausted",
                            str(result.get("error") or reason),
                        )
                        return result
                    record_failure("tool_handler_exception", type(exc).__name__)
                    raise
                with interrupt_lock:
                    interrupt_active = False
                timer.cancel()
                delay = retry_delay(plan, attempt_count - 1, retry_seed)
                if time.monotonic() + delay >= deadline:
                    from src.agent.control_plane_metrics import (
                        control_plane_metrics,
                    )

                    control_plane_metrics.increment(
                        "tool_retry", outcome="blocked_by_deadline"
                    )
                    side_effect_status = (
                        "may_have_occurred"
                        if contract.side_effect_type not in _READ_LIKE_EFFECTS
                        else "not_applicable"
                    )
                    result = self._deadline_result(
                        contract,
                        side_effect_status=side_effect_status,
                        effective_timeout_seconds=effective_timeout_seconds,
                    )
                    record_failure("tool_timeout", str(result["error"]))
                    return result
                from src.agent.control_plane_metrics import control_plane_metrics

                control_plane_metrics.increment("tool_retry", outcome="scheduled")
                time.sleep(delay)
                continue
            finally:
                with interrupt_lock:
                    interrupt_active = False
                timer.cancel()

            if deadline_fired or time.monotonic() >= deadline:
                if contract.side_effect_type in _READ_LIKE_EFFECTS:
                    output_error = self.validate_output(contract, result)
                    if output_error is None:
                        salvaged = self._salvage_late_read_result(
                            contract,
                            result,
                            effective_timeout_seconds=effective_timeout_seconds,
                        )
                        if salvaged is not None:
                            from src.agent.control_plane_metrics import (
                                control_plane_metrics,
                            )

                            control_plane_metrics.increment(
                                "late_read_salvage", outcome="partial"
                            )
                            record_failure(
                                "tool_timeout",
                                f"Tool '{contract.name}' returned a late read result",
                            )
                            return salvaged
                side_effect_status = (
                    "may_have_occurred"
                    if contract.side_effect_type not in _READ_LIKE_EFFECTS
                    else "not_applicable"
                )
                result = self._deadline_result(
                    contract,
                    side_effect_status=side_effect_status,
                    effective_timeout_seconds=effective_timeout_seconds,
                )
                record_failure("tool_timeout", str(result["error"]))
                return result
            output_error = self.validate_output(contract, result)
            if output_error is not None:
                record_failure(
                    "tool_output_schema_invalid",
                    str(output_error["error"]),
                )
                return output_error
            record_success()
            return result

        error = "Tool retry loop ended without a result"
        record_failure("tool_retry_loop_invalid", error)
        raise RuntimeError(error)
