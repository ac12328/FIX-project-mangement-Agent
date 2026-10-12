"""Deterministic JSON-schema validation helpers for tool contracts."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import asdict, is_dataclass
from datetime import date, datetime
from enum import Enum
from typing import Any

from jsonschema import Draft202012Validator
from jsonschema.exceptions import ValidationError
from pydantic import BaseModel


def json_compatible(value: Any) -> Any:
    """Convert common handler result types to JSON-schema-compatible values."""
    if isinstance(value, BaseModel):
        return json_compatible(value.model_dump(mode="json"))
    if is_dataclass(value) and not isinstance(value, type):
        return json_compatible(asdict(value))
    if isinstance(value, Enum):
        return json_compatible(value.value)
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, Mapping):
        return {str(key): json_compatible(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set, frozenset)):
        return [json_compatible(item) for item in value]
    return value


def _json_pointer(path: Any) -> str:
    parts = []
    for item in path:
        encoded = str(item).replace("~", "~0").replace("/", "~1")
        parts.append(encoded)
    return "$" if not parts else "$/" + "/".join(parts)


def _validation_message(error: ValidationError) -> str:
    """Return a deterministic message that never includes instance values."""
    keyword = str(error.validator or "schema")
    if keyword == "required":
        required = error.validator_value if isinstance(error.validator_value, list) else []
        present = error.instance if isinstance(error.instance, Mapping) else {}
        missing = [str(name) for name in required if name not in present]
        if missing:
            return "Missing required property: " + ", ".join(sorted(missing))
        return "A required property is missing"
    if keyword == "type":
        return f"Expected type: {error.validator_value}"
    if keyword == "enum":
        return "Value is not an allowed enum member"
    if keyword == "additionalProperties":
        return "Unexpected property is not allowed"
    if keyword == "minItems":
        return f"Array must contain at least {error.validator_value} item(s)"
    if keyword == "maxItems":
        return f"Array must contain at most {error.validator_value} item(s)"
    if keyword == "minLength":
        return f"String must contain at least {error.validator_value} character(s)"
    if keyword == "maxLength":
        return f"String must contain at most {error.validator_value} character(s)"
    if keyword in {"minimum", "exclusiveMinimum", "maximum", "exclusiveMaximum"}:
        return f"Numeric constraint failed: {keyword}"
    if keyword in {"anyOf", "oneOf", "allOf", "not"}:
        return f"Schema composition constraint failed: {keyword}"
    if keyword == "pattern":
        return "String format constraint failed"
    return f"Schema constraint failed: {keyword}"


def schema_errors(schema: Mapping[str, Any], instance: Any) -> list[dict[str, str]]:
    validator = Draft202012Validator(schema)
    errors = sorted(
        validator.iter_errors(instance),
        key=lambda error: (_json_pointer(error.absolute_path), str(error.validator)),
    )
    return [
        {
            "path": _json_pointer(error.absolute_path),
            "keyword": str(error.validator or "schema"),
            "message": _validation_message(error),
        }
        for error in errors
    ]


class ToolContractValidationError(ValueError):
    """Fail-closed input contract violation with a stable machine error code."""

    code = "tool_input_schema_invalid"

    def __init__(self, tool_name: str, details: list[dict[str, str]]) -> None:
        self.tool_name = tool_name
        self.details = details
        first = details[0] if details else {
            "path": "$", "message": "Input does not match the registered schema"
        }
        super().__init__(
            f"Tool '{tool_name}' input schema validation failed at "
            f"{first['path']}: {first['message']}"
        )

    def as_result(self) -> dict[str, Any]:
        return {
            "ok": False,
            "error": str(self),
            "error_code": self.code,
            "tool_name": self.tool_name,
            "schema_errors": self.details,
            "side_effect_status": "not_started",
        }
