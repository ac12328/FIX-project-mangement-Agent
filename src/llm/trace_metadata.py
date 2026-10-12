"""Normalize provider retry, failover, and cache metadata for LLM traces."""

from typing import Any


def _metadata_value(value: Any, key: str, default: Any = None) -> Any:
    """Read one metadata field from mapping, Pydantic, or object values."""
    if value is None:
        return default
    if isinstance(value, dict):
        return value.get(key, default)
    return getattr(value, key, default)


def _metadata_field(value: Any, key: str) -> tuple[Any, bool]:
    """Return a metadata value plus whether the provider exposed the field."""
    sentinel = object()
    found = _metadata_value(value, key, sentinel)
    return (None, False) if found is sentinel else (found, True)


def _usage_cache_metadata(
    usage: Any,
    *,
    explicit_capability: bool | None = None,
    explicit_hit: Any = None,
) -> dict[str, Any]:
    """Normalize cache usage and distinguish capability from reporting coverage."""
    details = _metadata_value(usage, "prompt_tokens_details")
    read_candidates = [
        _metadata_field(usage, "cache_read_input_tokens"),
        _metadata_field(usage, "cached_tokens"),
        _metadata_field(details, "cached_tokens"),
        _metadata_field(details, "cache_read_input_tokens"),
    ]
    write_candidates = [
        _metadata_field(usage, "cache_creation_input_tokens"),
        _metadata_field(usage, "cache_write_input_tokens"),
        _metadata_field(details, "cache_creation_input_tokens"),
        _metadata_field(details, "cache_write_input_tokens"),
    ]

    def _largest_non_negative(values: list[tuple[Any, bool]]) -> tuple[int, bool]:
        reported = any(present for _, present in values)
        normalized: list[int] = []
        for value, present in values:
            if not present or value is None:
                continue
            try:
                normalized.append(max(0, int(value)))
            except (TypeError, ValueError):
                continue
        return (max(normalized, default=0), reported)

    cache_read_tokens, read_reported = _largest_non_negative(read_candidates)
    cache_write_tokens, write_reported = _largest_non_negative(write_candidates)
    usage_hit, usage_hit_reported = _metadata_field(usage, "cache_hit")
    if explicit_hit is None and usage_hit_reported:
        explicit_hit = usage_hit
    hit_reported = explicit_hit is not None or usage_hit_reported
    usage_reported = read_reported or write_reported or hit_reported
    cache_hit = bool(explicit_hit) if explicit_hit is not None else None
    if cache_hit is None and (read_reported or write_reported):
        cache_hit = cache_read_tokens > 0

    if usage_reported:
        capability_status = "supported"
        reporting_status = (
            "reported_used"
            if bool(cache_hit) or cache_read_tokens > 0 or cache_write_tokens > 0
            else "reported_no_use"
        )
    elif explicit_capability is False:
        capability_status = "unsupported"
        reporting_status = "unsupported"
    else:
        capability_status = "supported" if explicit_capability is True else "unknown"
        reporting_status = "not_reported"

    return {
        "cache_hit": cache_hit,
        "cache_read_tokens": cache_read_tokens,
        "cache_write_tokens": cache_write_tokens,
        "cache_capability_status": capability_status,
        "cache_reporting_status": reporting_status,
    }


def _response_cache_metadata(response: Any) -> dict[str, Any]:
    """Derive cache capability from explicit provider metadata or reported usage fields."""
    hidden = _metadata_value(response, "_hidden_params", {})
    usage = _metadata_value(response, "usage")
    capability: bool | None = None
    for container in (response, hidden, usage):
        for key in ("cache_supported", "prompt_caching_supported"):
            value, present = _metadata_field(container, key)
            if present and value is not None:
                capability = bool(value)
                break
        if capability is not None:
            break
    explicit_hit = None
    for container in (response, hidden):
        value, present = _metadata_field(container, "cache_hit")
        if present:
            explicit_hit = value
            break
    return _usage_cache_metadata(
        usage,
        explicit_capability=capability,
        explicit_hit=explicit_hit,
    )


def _response_retry_count(response: Any) -> int:
    """Best-effort actual retry count from LiteLLM response metadata."""
    hidden = _metadata_value(response, "_hidden_params", {})
    for key in ("retry_count", "retries", "litellm_retry_count"):
        value = _metadata_value(hidden, key)
        if value is None:
            value = _metadata_value(response, key)
        if value is not None:
            try:
                return max(0, int(value))
            except (TypeError, ValueError):
                continue
    attempt_count = _metadata_value(hidden, "attempt_count")
    try:
        return max(0, int(attempt_count) - 1) if attempt_count is not None else 0
    except (TypeError, ValueError):
        return 0


def _response_trace_metadata(
    response: Any,
    *,
    requested_model: str | None = None,
    failover_from: str | None = None,
) -> dict[str, Any]:
    """Return normalized retry, failover, and cache metadata for a response."""
    response_model = str(_metadata_value(response, "model", "") or "")
    if failover_from is None and requested_model and response_model and response_model != requested_model:
        failover_from = requested_model
    return {
        "retry_count": _response_retry_count(response),
        "failover_from": failover_from,
        **_response_cache_metadata(response),
    }


