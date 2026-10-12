"""Pure domain mapping helpers — single source for cross-entity status/ball/task maps."""
from __future__ import annotations

from src.models.enums import BallWith, ProjectStatus, TicketStatus

# Re-export task taxonomy helpers (canonical definitions live in tasks.py)
from src.models.tasks import (  # noqa: F401
    LlmRoute,
    BUSINESS_TASK_TO_LLM_ROUTE,
    SPECIALIST_TO_BUSINESS_TASK,
    resolve_llm_route,
    specialist_to_business_task,
)

# Return plain str values for compatibility with callers/tests that compare to "closed"/"open".
WorkItemStatus = str  # "open" | "resolved" | "closed"


def is_project_terminal(status: ProjectStatus | str) -> bool:
    s = status if isinstance(status, ProjectStatus) else ProjectStatus(status)
    return s in ProjectStatus.terminal_statuses()


def project_status_to_work_item_status(status: ProjectStatus | str) -> WorkItemStatus:
    if is_project_terminal(status):
        return "closed"
    return "open"


def is_ticket_terminal(status: TicketStatus | str) -> bool:
    s = status if isinstance(status, TicketStatus) else TicketStatus(status)
    return s in TicketStatus.terminal_statuses()


def ticket_status_to_work_item_status(status: TicketStatus | str) -> WorkItemStatus:
    s = status if isinstance(status, TicketStatus) else TicketStatus(status)
    if s == TicketStatus.RESOLVED:
        return "resolved"
    if s == TicketStatus.CLOSED:
        return "closed"
    return "open"


_TICKET_BALL_LEGACY_TO_CANONICAL: dict[str, BallWith] = {
    "us": BallWith.INTERNAL,
    "client": BallWith.EXTERNAL,
    "resolved": BallWith.UNKNOWN,
    "unknown": BallWith.UNKNOWN,
}


def ticket_legacy_ball_to_ball_with(legacy: str) -> BallWith:
    return _TICKET_BALL_LEGACY_TO_CANONICAL.get(legacy, BallWith.UNKNOWN)


def ball_with_to_ticket_legacy(ball: BallWith | str) -> str:
    """Compat reverse map for one release of dashboard if needed."""
    b = ball if isinstance(ball, BallWith) else BallWith(ball)
    if b == BallWith.INTERNAL:
        return "us"
    if b == BallWith.EXTERNAL:
        return "client"
    return "unknown"
