"""LiteLLM request shapes shared by the facade call paths.

These builders only construct kwargs. Call-time routing, budgeting, provider calls,
tracing and failover remain with the facade so its patch points stay intact.
"""

from typing import Any

from src.llm.routing import ModelConfig


def build_completion_kwargs(
    *,
    config: ModelConfig,
    model: str,
    messages: list[dict[str, Any]],
    temperature: float,
    max_tokens: int | None,
    model_override: str | None,
    tools: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Primary non-stream request, including structured-mode base requests."""
    kwargs: dict[str, Any] = dict(model=model, messages=messages)
    if tools is not None:
        kwargs["tools"] = tools
    kwargs["temperature"] = temperature
    kwargs["max_tokens"] = max_tokens
    if not model_override:
        kwargs["fallbacks"] = config.fallbacks
    if config.api_base:
        kwargs["api_base"] = config.api_base
    if config.api_key:
        kwargs["api_key"] = config.api_key
    if config.max_retries:
        kwargs["max_retries"] = config.max_retries
    return kwargs


def build_backup_kwargs(
    *,
    config: ModelConfig,
    messages: list[dict[str, Any]],
    temperature: float,
    max_tokens: int | None,
    tools: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Backup non-stream requests pass endpoint and retry values even when null."""
    kwargs: dict[str, Any] = dict(model=config.primary, messages=messages)
    if tools is not None:
        kwargs["tools"] = tools
    kwargs.update(
        temperature=temperature,
        max_tokens=max_tokens,
        api_base=config.api_base,
        api_key=config.api_key,
        max_retries=config.max_retries,
    )
    return kwargs


def build_stream_kwargs(
    *,
    config: ModelConfig,
    messages: list[dict[str, Any]],
    temperature: float,
    max_tokens: int | None,
    timeout_sec: int | float,
    backup: bool = False,
) -> dict[str, Any]:
    """Streaming requests use HTTP timeout, not watchdog or retry/fallback kwargs."""
    kwargs: dict[str, Any] = dict(
        model=config.primary,
        messages=messages,
        temperature=temperature,
        max_tokens=max_tokens,
        stream=True,
        stream_options={"include_usage": True},
        timeout=timeout_sec,
    )
    # Backup historically sends even empty endpoint values; primary omits them.
    if backup or config.api_base:
        kwargs["api_base"] = config.api_base
    if backup or config.api_key:
        kwargs["api_key"] = config.api_key
    return kwargs


def add_json_mode(kwargs: dict[str, Any]) -> None:
    """Apply the native JSON mode suffix to an existing primary request."""
    kwargs["response_format"] = {"type": "json_object"}


def add_structured_tool(kwargs: dict[str, Any], tools: list[dict[str, Any]]) -> None:
    """Apply the required tool suffix to an existing primary request."""
    kwargs["tools"] = tools
    kwargs["tool_choice"] = "required"
