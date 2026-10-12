"""Execution-boundary authorization for tools with side effects."""

from __future__ import annotations

from dataclasses import dataclass

from src.tools.contracts import ToolContract


@dataclass(frozen=True)
class ToolExecutionContext:
    operator_id: str | None = None
    surface: str = "internal"
    role: str | None = None
    specialist_type: str | None = None
    approved_action_id: str | None = None


def authorize_tool_call(
    contract: ToolContract,
    context: ToolExecutionContext,
) -> str | None:
    """Return an authorization error, or None when execution is allowed.

    Existing project/domain write tools retain their established specialist and
    write-layer guards. External tools are fail-closed here and must present an
    approved action ID before a handler can run.
    """
    if contract.side_effect_type != "external":
        return None
    if contract.requires_operator_approval and not context.approved_action_id:
        return f"Tool '{contract.name}' requires an approved action intent"
    return None
