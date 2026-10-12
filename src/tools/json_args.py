"""Parse and repair LLM tool-call argument JSON."""

from __future__ import annotations

import json
import logging
import re

logger = logging.getLogger(__name__)

# Strip a leading ```json / ``` fence (and trailing fence) that some local
# models emit around structured tool arguments, before any json.loads attempt.
_FENCE_OPEN_RE = re.compile(r"^\s*```(?:json|JSON)?\s*\n?", re.IGNORECASE)
_FENCE_CLOSE_RE = re.compile(r"\n?```\s*$")


def _strip_code_fence(text: str) -> str:
    stripped = _FENCE_OPEN_RE.sub("", text, count=1)
    if stripped != text:
        stripped = _FENCE_CLOSE_RE.sub("", stripped)
    return stripped


def parse_tool_arguments(raw_args: str | dict) -> dict:
    """Parse tool arguments; attempt repair when the model truncates mid-string."""
    if isinstance(raw_args, dict):
        return dict(raw_args)
    text = (raw_args or "").strip() or "{}"
    # Local models sometimes wrap the arguments in a markdown code fence; the
    # repair function below bails when the input does not start with '{', so
    # strip fences first or the whole tool call is dropped as invalid args.
    text = _strip_code_fence(text).strip() or "{}"
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError as exc:
        repaired = _repair_truncated_json_object(text)
        if repaired is not None:
            logger.warning("Repaired truncated tool arguments JSON")
            return repaired
        raise exc
    if not isinstance(parsed, dict):
        raise json.JSONDecodeError("tool arguments must be a JSON object", text, 0)
    return parsed


def _repair_truncated_json_object(raw: str) -> dict | None:
    if not raw.startswith("{"):
        return None
    candidates = [raw]
    if raw.count('"') % 2 == 1:
        candidates.append(raw + '"')
    for base in candidates:
        for suffix in ("}", '"}', '"}', '""}'):
            try:
                parsed = json.loads(base + suffix)
            except json.JSONDecodeError:
                continue
            if isinstance(parsed, dict):
                return parsed
    return None