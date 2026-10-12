"""Schema and metadata checks used by tool startup integrity validation."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from numbers import Real
from typing import Any

from jsonschema import Draft202012Validator
from jsonschema.exceptions import SchemaError

from src.tools.contracts import ToolContract

IssueSink = Callable[[str, str, str, str], None]

_ALLOWED_SIDE_EFFECTS = frozenset({"none", "read", "write", "external"})
_ALLOWED_RETRY_POLICIES = frozenset({"none", "once", "exponential"})
_ALLOWED_IDEMPOTENCY_MODES = frozenset({"none", "trusted_key"})
_ALLOWED_FALLBACKS = frozenset({"return_error", "skip", "use_cache"})
_BOOLEAN_METADATA_FIELDS = (
    "requires_operator_approval",
    "llm_visible",
    "log_input",
    "log_output",
    "audit_required",
    "query_safe",
)


def _audit_schema(
    add_issue: IssueSink,
    *,
    tool_name: str,
    schema_name: str,
    schema: Any,
    require_properties: bool,
) -> None:
    subject = f"tool:{tool_name}.{schema_name}"
    if not isinstance(schema, Mapping):
        add_issue(
            "invalid_schema",
            subject,
            "schema must be a mapping",
            f"Define {schema_name} as a valid JSON Schema object.",
        )
        return

    try:
        Draft202012Validator.check_schema(schema)
    except SchemaError as exc:
        add_issue(
            "invalid_schema",
            subject,
            f"invalid JSON Schema ({exc.message})",
            f"Correct the {schema_name} JSON Schema definition.",
        )

    if schema.get("type") != "object":
        add_issue(
            "invalid_schema_shape",
            subject,
            'top-level type must be "object"',
            f'Set {schema_name}["type"] to "object".',
        )

    properties = schema.get("properties", {})
    if not isinstance(properties, Mapping):
        add_issue(
            "invalid_schema_shape",
            subject,
            '"properties" must be a mapping when present',
            f"Correct the {schema_name} properties definition.",
        )
        properties = {}
    elif require_properties and "properties" not in schema:
        add_issue(
            "invalid_schema_shape",
            subject,
            'input schema must declare a "properties" mapping',
            "Define the accepted input properties explicitly.",
        )

    required = schema.get("required", [])
    if not isinstance(required, list) or any(
        not isinstance(name, str) or not name for name in required
    ):
        add_issue(
            "invalid_schema_required",
            subject,
            '"required" must be a list of non-empty property names',
            "Use property names declared in the schema properties mapping.",
        )
        return

    unknown_required = sorted(set(required) - set(properties))
    if unknown_required:
        add_issue(
            "unknown_required_property",
            subject,
            "required names are missing from properties: "
            + ", ".join(unknown_required),
            "Declare each required property or remove it from required.",
        )


def _audit_metadata(
    add_issue: IssueSink,
    contract: ToolContract,
    tool_name: str,
) -> None:
    subject = f"tool:{tool_name}"
    if not isinstance(contract.description, str) or not contract.description.strip():
        add_issue(
            "invalid_contract_metadata",
            subject,
            "description must be a non-empty string",
            "Add a concise model-facing tool description.",
        )
    if not isinstance(contract.version, str) or not contract.version.strip():
        add_issue(
            "invalid_contract_metadata",
            subject,
            "version must be a non-empty string",
            "Set an explicit contract version.",
        )

    for field_name in ("allowed_task_types", "allowed_contexts"):
        value = getattr(contract, field_name)
        if (
            not isinstance(value, list)
            or not value
            or any(not isinstance(item, str) or not item for item in value)
        ):
            add_issue(
                "invalid_contract_metadata",
                subject,
                f"{field_name} must be a non-empty list of non-empty strings",
                f"Correct {field_name} for this contract.",
            )

    enum_checks = (
        (
            "side_effect_type",
            contract.side_effect_type,
            _ALLOWED_SIDE_EFFECTS,
            "invalid_side_effect_type",
        ),
        (
            "retry_policy",
            contract.retry_policy,
            _ALLOWED_RETRY_POLICIES,
            "invalid_retry_policy",
        ),
        (
            "idempotency_mode",
            contract.idempotency_mode,
            _ALLOWED_IDEMPOTENCY_MODES,
            "invalid_idempotency_mode",
        ),
        (
            "fallback_behaviour",
            contract.fallback_behaviour,
            _ALLOWED_FALLBACKS,
            "invalid_fallback_behaviour",
        ),
    )
    for field_name, value, allowed, code in enum_checks:
        if value not in allowed:
            add_issue(
                code,
                subject,
                f"unsupported {field_name} value {value!r}",
                "Use one of: " + ", ".join(sorted(allowed)) + ".",
            )

    timeout = contract.timeout_seconds
    if isinstance(timeout, bool) or not isinstance(timeout, Real) or timeout <= 0:
        add_issue(
            "invalid_timeout",
            subject,
            f"timeout_seconds must be positive, got {timeout!r}",
            "Set a positive execution deadline.",
        )

    confidence = contract.minimum_context_confidence
    if confidence is not None and (
        isinstance(confidence, bool)
        or not isinstance(confidence, Real)
        or not 0 <= confidence <= 1
    ):
        add_issue(
            "invalid_context_confidence",
            subject,
            f"minimum_context_confidence must be within 0..1, got {confidence!r}",
            "Set confidence to None or a numeric value from 0 through 1.",
        )

    for field_name in _BOOLEAN_METADATA_FIELDS:
        if not isinstance(getattr(contract, field_name), bool):
            add_issue(
                "invalid_contract_metadata",
                subject,
                f"{field_name} must be boolean",
                f"Set {field_name} to True or False.",
            )

    if (
        contract.side_effect_type in {"write", "external"}
        and contract.retry_policy != "none"
        and contract.idempotency_mode != "trusted_key"
    ):
        add_issue(
            "unsafe_retry_contract",
            subject,
            "side-effecting retries require trusted-key idempotency",
            "Disable retries or set idempotency_mode to trusted_key.",
        )


def audit_contract_definition(
    contract: ToolContract,
    tool_name: str,
    add_issue: IssueSink,
) -> None:
    """Validate one contract without depending on integrity report classes."""
    _audit_schema(
        add_issue,
        tool_name=tool_name,
        schema_name="input_schema",
        schema=contract.input_schema,
        require_properties=True,
    )
    _audit_schema(
        add_issue,
        tool_name=tool_name,
        schema_name="output_schema",
        schema=contract.output_schema,
        require_properties=False,
    )
    _audit_metadata(add_issue, contract, tool_name)
