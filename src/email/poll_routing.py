"""Email poll routing precedence — triage class vs support-ticket thread match."""

from __future__ import annotations

from typing import Protocol


class _ClassifiedEventLike(Protocol):
    classification_type: str
    project_id: str | None


def prefer_project_update_routing(event: _ClassifiedEventLike) -> bool:
    """Return True when the email should use project_update agent routing.

    Resolved project_update emails take precedence over support-ticket
    thread notifications so onboarding mail is analysed by the
    project copilot even when the thread is also tracked as a support ticket.
    """
    return event.classification_type == "project_update" and bool(event.project_id)