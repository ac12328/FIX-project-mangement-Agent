"""Request sizing and context-window trimming for the LLM facade.

This module is transport-independent. The client passes configuration, logger,
and call-time functions so existing public/private facade patch points survive.
"""

from __future__ import annotations

import json
import logging
from typing import Any, Callable


def content_char_cost(content: Any) -> int:
    """Measure text and multimodal parts, including base64 image URLs."""
    if content is None:
        return 0
    if isinstance(content, str):
        return len(content)
    if isinstance(content, list):
        total = 0
        for part in content:
            if not isinstance(part, dict):
                total += len(str(part))
                continue
            text = part.get("text")
            if text:
                total += len(text)
            img = part.get("image_url")
            if isinstance(img, dict):
                total += len(img.get("url", "") or "")
            elif isinstance(img, str):
                total += len(img)
        return total
    return len(str(content))


def prompt_char_count(
    messages: list[dict[str, Any]],
    tools: list[dict[str, Any]] | None = None,
    *,
    content_cost: Callable[[Any], int] = content_char_cost,
) -> int:
    """Return prompt/tool size without exposing their contents in telemetry."""
    chars = sum(content_cost(message.get("content")) for message in messages)
    if tools:
        chars += sum(
            len(json.dumps(tool, ensure_ascii=False, default=str)) for tool in tools
        )
    return chars


def estimate_tokens(
    messages: list[dict],
    tools: list[dict] | None = None,
    *,
    char_count: Callable[..., int] = prompt_char_count,
    margin: int = 300,
) -> int:
    return int(char_count(messages, tools) / 3.5) + margin


def cap_max_tokens(
    requested: int | None,
    messages: list[dict],
    tools: list[dict] | None = None,
    *,
    context_window: int,
    min_output_tokens: int,
    estimate: Callable[..., int],
    log: logging.Logger,
) -> int | None:
    """Reduce max_tokens if estimated input plus output exceeds the window."""
    if requested is None:
        return None
    estimated_input = estimate(messages, tools)
    available = context_window - estimated_input
    if available < min_output_tokens:
        available = min_output_tokens
    if requested > available:
        log.warning(
            "Capping max_tokens %d ??%d (est input=%d, window=%d)",
            requested,
            available,
            estimated_input,
            context_window,
        )
        return available
    return requested


def safe_truncate_content(content: str, max_chars: int) -> str:
    """Truncate content without breaking JSON tool payloads."""
    if len(content) <= max_chars:
        return content
    stripped = content.lstrip()
    if stripped.startswith("{") or stripped.startswith("["):
        preview = content[: max(0, max_chars - 80)]
        return json.dumps({"truncated": True, "preview": preview}, ensure_ascii=False)
    return content[:max_chars] + "\n[...truncated]"


def _truncate_text_parts(
    content: Any,
    max_chars: int,
    *,
    content_cost: Callable[[Any], int],
    truncate: Callable[[str, int], str],
) -> Any:
    """Trim only text, retaining multimodal part metadata and image bytes."""
    if isinstance(content, str):
        return truncate(content, max_chars)
    if not isinstance(content, list):
        return content
    text_parts = [
        (i, part)
        for i, part in enumerate(content)
        if isinstance(part, dict)
        and part.get("type") == "text"
        and isinstance(part.get("text"), str)
    ]
    if not text_parts:
        return content
    text_cost = sum(content_cost(part["text"]) for _, part in text_parts)
    fixed_cost = max(0, content_cost(content) - text_cost)
    remaining = max(0, max_chars - fixed_cost)
    result = list(content)
    for i, part in text_parts:
        text = part["text"]
        if content_cost(text) > remaining:
            text = truncate(text, remaining)
            result[i] = {**part, "text": text}
        remaining = max(0, remaining - content_cost(text))
    return result


def trim_messages_to_fit(
    messages: list[dict],
    tools: list[dict] | None = None,
    reserve_output: int = 512,
    *,
    context_window: int,
    estimate: Callable[..., int],
    content_cost: Callable[[Any], int],
    truncate: Callable[[str, int], str],
    log: logging.Logger,
) -> list[dict]:
    """Trim older context/text without mutating inputs; deny an unfit request."""
    budget = context_window - reserve_output
    est = estimate(messages, tools)
    if est <= budget:
        return messages
    log.warning(
        "Context overflow: est=%d tokens, budget=%d - trimming messages",
        est,
        budget,
    )

    result = list(messages)
    if len(result) > 5:
        keep_head = result[:1]
        keep_tail = result[-4:]
        middle = result[1:-4]
        while est > budget and middle:
            middle.pop(0)
            result = keep_head + middle + keep_tail
            est = estimate(result, tools)
        if est <= budget:
            return result

    # Keep the historical first text allowance and newest-first edit order.
    # Further bounded passes account for multiple text parts, immutable images,
    # tool schemas, estimator overhead and the truncation marker itself.
    max_chars = max(0, int(budget * 3.5 * 0.3))
    while True:
        for i in range(len(result) - 1, -1, -1):
            content = result[i].get("content")
            cost = content_cost(content)
            if cost <= max_chars:
                continue
            shortened = _truncate_text_parts(
                content,
                max_chars,
                content_cost=content_cost,
                truncate=truncate,
            )
            shortened_cost = content_cost(shortened)
            if shortened_cost >= cost:
                continue
            result[i] = {**result[i], "content": shortened}
            log.warning(
                "Truncated message %d (cost %d -> %d chars)",
                i,
                cost,
                shortened_cost,
            )
            est = estimate(result, tools)
            if est <= budget:
                return result
        if max_chars == 0:
            break
        max_chars //= 2

    raise ValueError(
        "LLM request exceeds context budget after text trimming "
        f"(estimated_input={est}, input_budget={budget}, "
        f"context_window={context_window}, reserve_output={reserve_output}); "
        "non-trimmable payload/tool schema or minimum safe text cannot fit"
    )
