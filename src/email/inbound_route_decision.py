"""Pure first-pass routing decisions for classified inbound mail.

No persistence, agent invocation, or presentation occurs here. The caller
supplies detectors to retain its existing patch seams and policy version.
"""
from __future__ import annotations

from typing import Any, Callable, Literal

from pydantic import BaseModel, ConfigDict

RouteKind = Literal[
    "delivery_failure", "operational", "internal", "spam", "support",
    "project_update", "operator_review", "suppress",
]


class InboundRouteDecision(BaseModel):
    model_config = ConfigDict(frozen=True)

    route: RouteKind
    classification: str
    requires_operator_triage: bool
    should_notify_operator: bool


def _bool_attr(event: Any, name: str) -> bool:
    value = getattr(event, name, False)
    return value if isinstance(value, bool) else False


def decide_inbound_route(
    event: Any,
    body: str,
    *,
    delivery_failure_detector: Callable[[str, str, str], bool],
    support_signal_counter: Callable[[str, str], int],
    support_signal_threshold: int,
) -> InboundRouteDecision:
    """Choose a route without mutating the event or performing external work."""
    classification = event.classification_type
    requires_triage = _bool_attr(event, "requires_operator_triage")
    should_notify = _bool_attr(event, "should_notify_operator")
    if classification == "new_inquiry":
        should_notify = True

    def result(route: RouteKind) -> InboundRouteDecision:
        return InboundRouteDecision(
            route=route,
            classification=classification,
            requires_operator_triage=requires_triage,
            should_notify_operator=should_notify,
        )

    # Delivery reports take precedence over every other classification.
    if delivery_failure_detector(event.sender_email or "", event.subject or "", body):
        return result("delivery_failure")
    if classification == "operational":
        return result("operational")
    if classification == "internal":
        return result("internal")
    if classification == "spam":
        # Deterministic support evidence overrides a spam classification.
        if support_signal_counter(event.subject or "", body) >= support_signal_threshold:
            classification = "support_issue"
        else:
            return result("spam")
    if classification in ("support_issue", "project_support"):
        return result("support")
    if classification == "project_update" and event.project_id and not requires_triage:
        return result("project_update")
    if should_notify or requires_triage:
        return result("operator_review")
    return result("suppress")
