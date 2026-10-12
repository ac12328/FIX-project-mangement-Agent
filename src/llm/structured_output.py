"""Pure structured-output schema prompting and JSON parsing.

Provider fallback and trace ownership remain in ``src.llm.client``. The
optional logger preserves that facade's privacy-safe diagnostic channel.
"""
from __future__ import annotations

import hashlib
import json
import logging
import re
from dataclasses import dataclass
from typing import Any, Callable

logger = logging.getLogger(__name__)


class LLMParseFailure(ValueError):
    """Raised when an LLM response cannot be parsed into the expected JSON object."""


@dataclass(frozen=True)
class StructuredToolMessage:
    """Extracted provider output, retaining the raw text for tracing before parsing."""

    raw_output: str
    tool_call_count: int
    _content: Any
    _arguments: Any = None

    def parse(self, task: str, parse_json: Callable[[str], dict]) -> dict:
        """Parse using the facade's call-time JSON parser (and its logger)."""
        if not self.tool_call_count:
            if self._content:
                return parse_json(self._content)
            raise LLMParseFailure(f"LLM did not return a {task} tool call")
        if isinstance(self._arguments, dict):
            return self._arguments
        return parse_json(self._arguments or "")


def _structured_tool_message(message: Any) -> StructuredToolMessage:
    """Normalize one provider tool message without policy, logging or I/O."""
    tool_calls = getattr(message, "tool_calls", None) or []
    content = getattr(message, "content", None) or ""
    if tool_calls:
        arguments = tool_calls[0].function.arguments
        raw_output = (
            json.dumps(arguments, ensure_ascii=False, default=str)
            if isinstance(arguments, dict)
            else str(arguments or "")
        )
    else:
        arguments = None
        raw_output = content
    return StructuredToolMessage(raw_output, len(tool_calls), content, arguments)


def _normalize_response_schema(response_schema: dict[str, Any]) -> dict[str, Any]:
    """Convert shorthand response schemas into JSON Schema objects."""
    if response_schema.get("type") == "object":
        return response_schema

    properties: dict[str, Any] = {}
    required: list[str] = []
    for key, value in response_schema.items():
        properties[key] = _infer_schema_property(value)
        required.append(key)
    return {
        "type": "object",
        "properties": properties,
        "required": required,
    }


def _uses_grok_46_schema_prompt(model: str | None) -> bool:
    """Return whether the selected proxy model needs the prose schema contract."""
    if not model:
        return False
    model_name = model.rsplit("/", 1)[-1].split(":", 1)[0].casefold()
    return model_name == "grok-4.6" or model_name.startswith("grok-4.6-")


def _resolve_local_schema_ref(
    node: dict[str, Any],
    root_schema: dict[str, Any],
) -> dict[str, Any]:
    """Resolve one local JSON Schema reference while retaining sibling constraints."""
    ref = node.get("$ref")
    if not isinstance(ref, str) or not ref.startswith("#/"):
        return node
    resolved: Any = root_schema
    try:
        for part in ref[2:].split("/"):
            key = part.replace("~1", "/").replace("~0", "~")
            resolved = resolved[key]
    except (KeyError, TypeError):
        return node
    if not isinstance(resolved, dict):
        return node
    return {**resolved, **{key: value for key, value in node.items() if key != "$ref"}}


def _schema_type_label(node: dict[str, Any], root_schema: dict[str, Any]) -> str:
    """Describe one schema node without embedding raw JSON Schema syntax."""
    node = _resolve_local_schema_ref(node, root_schema)
    if "const" in node:
        return f"exactly {node['const']!r}"
    enum = node.get("enum")
    if isinstance(enum, list) and enum:
        return "one of " + ", ".join(repr(value) for value in enum)
    for keyword in ("anyOf", "oneOf"):
        options = node.get(keyword)
        if isinstance(options, list) and options:
            labels = dict.fromkeys(
                _schema_type_label(option, root_schema)
                for option in options
                if isinstance(option, dict)
            )
            return " or ".join(labels) or "value"
    node_type = node.get("type")
    if isinstance(node_type, list):
        return " or ".join(str(value) for value in node_type)
    if node_type == "array":
        items = node.get("items")
        item_label = (
            _schema_type_label(items, root_schema)
            if isinstance(items, dict)
            else "value"
        )
        return f"array of {item_label}"
    if node_type == "object" or "properties" in node:
        return "object"
    return str(node_type or "value")


def _grok_46_schema_contract(response_schema: dict[str, Any]) -> str:
    """Render a field contract that avoids Grok 4.6 raw-schema safety false positives."""
    root_schema = _normalize_response_schema(response_schema)
    lines: list[str] = []

    def visit(node: dict[str, Any], path: str, depth: int) -> None:
        if depth > 8 or len(lines) >= 200:
            return
        node = _resolve_local_schema_ref(node, root_schema)
        properties = node.get("properties")
        if not isinstance(properties, dict):
            return
        required = set(node.get("required") or ())
        for name, child in properties.items():
            if not isinstance(child, dict):
                continue
            resolved = _resolve_local_schema_ref(child, root_schema)
            child_path = f"{path}.{name}" if path else name
            requirement = "required" if name in required else "optional"
            lines.append(
                f"- {child_path}: {_schema_type_label(resolved, root_schema)}; "
                f"{requirement}."
            )
            nested = resolved
            nested_path = child_path
            if resolved.get("type") == "array":
                items = resolved.get("items")
                if isinstance(items, dict):
                    nested = _resolve_local_schema_ref(items, root_schema)
                    nested_path += "[]"
            if nested.get("type") == "object" or "properties" in nested:
                visit(nested, nested_path, depth + 1)

    visit(root_schema, "", 0)
    if not lines:
        lines.append(f"- Root value: {_schema_type_label(root_schema, root_schema)}.")
    return (
        "\n\nReturn one valid JSON object. Response contract:\n"
        + "\n".join(lines)
        + "\nUse JSON null for absent nullable values. "
        "Do not add fields outside this contract."
    )


def _augment_messages_with_schema(
    messages: list[dict[str, Any]],
    response_schema: dict[str, Any],
    *,
    model: str | None = None,
) -> list[dict[str, Any]]:
    if _uses_grok_46_schema_prompt(model):
        schema_instruction = _grok_46_schema_contract(response_schema)
    else:
        schema_instruction = (
            "\n\nYou MUST respond with valid JSON matching this schema:\n"
            f"```json\n{json.dumps(response_schema, indent=2)}\n```\n"
            "Return ONLY the JSON object, no other text."
        )
    augmented_messages = list(messages)
    if augmented_messages and augmented_messages[0]["role"] == "system":
        content = augmented_messages[0]["content"]
        # Handle multimodal content (list of parts) vs plain string
        if isinstance(content, list):
            new_parts = list(content)
            # Append schema to the last text part
            for i in range(len(new_parts) - 1, -1, -1):
                if isinstance(new_parts[i], dict) and new_parts[i].get("type") == "text":
                    new_parts[i] = {**new_parts[i], "text": new_parts[i]["text"] + schema_instruction}
                    break
            else:
                new_parts.append({"type": "text", "text": schema_instruction})
            augmented_messages[0] = {"role": "system", "content": new_parts}
        else:
            augmented_messages[0] = {
                "role": "system",
                "content": content + schema_instruction,
            }
    else:
        augmented_messages.insert(0, {
            "role": "system",
            "content": schema_instruction,
        })
    return augmented_messages


def _infer_schema_property(value: Any) -> dict[str, Any]:
    if isinstance(value, bool):
        return {"type": "boolean"}
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return {"type": "number"}
    if value is None:
        return {"type": ["string", "null"]}
    if isinstance(value, str):
        if "|" in value:
            return {"type": "string", "enum": [part.strip() for part in value.split("|") if part.strip()]}
        if "null" in value.lower():
            return {"type": ["string", "null"]}
        return {"type": "string"}
    return {"type": "string"}


def _parse_json_response(raw: str, *, log: logging.Logger | None = None) -> dict:
    raw = (raw or "").strip()
    if raw.startswith("```json"):
        raw = raw[7:]
    if raw.startswith("```"):
        raw = raw[3:]
    if raw.endswith("```"):
        raw = raw[:-3]
    raw = raw.strip()

    try:
        return json.loads(raw)
    except json.JSONDecodeError as first_error:
        match = re.search(r"\{.*?\}", raw, re.DOTALL)
        if match:
            try:
                return json.loads(match.group(0))
            except json.JSONDecodeError:
                pass
        response_hash = hashlib.sha256(raw.encode("utf-8")).hexdigest()
        (log or logger).error(
            "Structured output parse failed: error=%s line=%d column=%d "
            "response_chars=%d response_hash=sha256:%s",
            first_error.msg,
            first_error.lineno,
            first_error.colno,
            len(raw),
            response_hash,
        )
        raise LLMParseFailure(f"LLM returned invalid JSON: {first_error}") from first_error


