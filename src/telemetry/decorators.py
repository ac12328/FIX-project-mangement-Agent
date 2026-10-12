"""
@traced decorator for automatic span creation.

Usage:
    from src.telemetry.decorators import traced

    @traced("write_layer")
    def process(self, proposed_update):
        ...

    @traced()   # defaults to function name
    def my_function():
        ...
"""

from __future__ import annotations

import functools
from typing import Any, Callable

from opentelemetry import trace


def traced(
    span_name: str | None = None,
    *,
    attributes: dict[str, Any] | None = None,
) -> Callable:
    """Decorator that wraps a function in an OpenTelemetry span.

    Args:
        span_name: Name for the span. Defaults to "module.function".
        attributes: Static attributes to attach to every span.
    """

    def decorator(fn: Callable) -> Callable:
        name = span_name or f"{fn.__module__}.{fn.__qualname__}"

        @functools.wraps(fn)
        def wrapper(*args: Any, **kwargs: Any) -> Any:
            tracer = trace.get_tracer("agentfixbpro")
            with tracer.start_as_current_span(name) as span:
                if attributes:
                    for k, v in attributes.items():
                        span.set_attribute(k, v)
                try:
                    result = fn(*args, **kwargs)
                    span.set_status(trace.StatusCode.OK)
                    return result
                except Exception as exc:
                    span.set_status(
                        trace.StatusCode.ERROR, str(exc)
                    )
                    span.record_exception(exc)
                    raise

        return wrapper

    # Allow @traced without parentheses
    if callable(span_name):
        fn = span_name
        span_name = None
        return traced()(fn)

    return decorator
