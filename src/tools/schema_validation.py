"""Privacy-safe JSON Schema validation diagnostics for model tool arguments."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from jsonschema import Draft202012Validator, FormatChecker
from pydantic import ValidationError

_MAX_DIAGNOSTICS = 12


def _path(parts: Sequence[Any]) -> str:
    rendered = ".".join(str(part) for part in parts)
    return rendered or "<root>"


def json_schema_diagnostics(
    instance: Any,
    schema: Mapping[str, Any],
    *,
    max_items: int = _MAX_DIAGNOSTICS,
) -> list[str]:
    """Return bounded path/type diagnostics without echoing input values."""
    validator = Draft202012Validator(dict(schema), format_checker=FormatChecker())
    errors = sorted(
        validator.iter_errors(instance),
        key=lambda error: (
            tuple(str(part) for part in error.absolute_path),
            str(error.validator or ""),
        ),
    )
    diagnostics: list[str] = []
    for error in errors:
        base_path = list(error.absolute_path)
        if error.validator == "required" and isinstance(error.instance, Mapping):
            required = error.validator_value or []
            for field_name in required:
                if field_name not in error.instance:
                    item = f"{_path([*base_path, field_name])}:missing"
                    if item not in diagnostics:
                        diagnostics.append(item)
                    if len(diagnostics) >= max_items:
                        return diagnostics
            continue

        code = {
            "additionalProperties": "unexpected_field",
            "type": "type",
            "enum": "enum",
            "minimum": "range",
            "maximum": "range",
            "exclusiveMinimum": "range",
            "exclusiveMaximum": "range",
            "minLength": "too_short",
            "maxLength": "too_long",
            "minItems": "too_few_items",
            "maxItems": "too_many_items",
            "pattern": "pattern",
            "format": "format",
        }.get(str(error.validator), "schema")
        item = f"{_path(base_path)}:{code}"
        if item not in diagnostics:
            diagnostics.append(item)
        if len(diagnostics) >= max_items:
            break
    return diagnostics


def pydantic_validation_diagnostics(
    error: ValidationError,
    *,
    max_items: int = _MAX_DIAGNOSTICS,
) -> list[str]:
    """Return bounded Pydantic error paths/types without input or messages."""
    diagnostics: list[str] = []
    for item in error.errors(include_url=False, include_input=False):
        location = _path(item.get("loc", ()))
        error_type = str(item.get("type") or "validation_error")
        diagnostic = f"{location}:{error_type}"
        if diagnostic not in diagnostics:
            diagnostics.append(diagnostic)
        if len(diagnostics) >= max_items:
            break
    return diagnostics
