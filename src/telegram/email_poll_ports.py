"""Call-time dependencies for the scheduled email poll adapter."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable


@dataclass(frozen=True)
class EmailPollPorts:
    """Runtime dependencies supplied by the legacy scheduler facade."""
    scoped_read: Callable[..., Any]
    scoped_write: Callable[..., Any]
    logger: Any
    build_email_classifier: Callable[..., Any]
    route_inbound_event: Callable[..., Any]
    should_run_inbound_reply_agent: Callable[..., Any]
    prefer_project_update_routing: Callable[..., Any]
    build_card: Callable[..., Any]
    format_card_text: Callable[..., Any]
    t: Callable[..., Any]
    card_to_markup: Callable[..., Any]
    send_persisted_message: Callable[..., Any]
    with_async_retry: Callable[..., Any]
    _mark_job_success: Callable[..., Any]
    _notify_job_failure: Callable[..., Any]
    _YIELD_EVERY: int
    _summarize_email_for_card: Callable[..., Any]
    _process_inbound_reply_via_agent: Callable[..., Any]


