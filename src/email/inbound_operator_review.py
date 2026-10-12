"""Operator-review routing after classification, with explicit policy boundaries.

The owner supplies write scopes and audit callbacks so owned and caller-owned
inbound routes retain their existing transaction and post-commit behavior.
"""
from __future__ import annotations

import logging
import sqlite3
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, ContextManager

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class OperatorReviewPorts:
    write_scope: Callable[[], ContextManager[sqlite3.Connection]]
    already_queued: Callable[[sqlite3.Connection, str], bool]
    record_activity: Callable[..., None]
    window_open: Callable[[], bool]
    build_notification: Callable[..., dict]
    translate: Callable[[str], str]
    group_notification: Callable[..., dict] | None = None


def route_operator_review(
    event: Any,
    body: str,
    *,
    classification: str,
    ports: OperatorReviewPorts,
) -> dict | None:
    """Queue at most one operator card or digest for an inbound email.

    The first dedup read is only an optimization. The final read and the
    card/digest intent must share a write scope, including caller-owned scopes.
    """
    with ports.write_scope() as conn:
        if ports.already_queued(conn, event.email_id):
            logger.debug("Operator notification already queued for %s", event.email_id)
            return None

    if classification == "ambiguous":
        from src.triage.email_triage import has_fix_signal

        if not has_fix_signal(event.subject or "", body):
            event.suppressed_reason = "ambiguous_no_fix_signal"
            with ports.write_scope() as conn:
                ports.record_activity(conn, event, action_type="email_notification_suppressed")
            return None

        from src.repositories.digest_queue_repo import enqueue_digest_item

        with ports.write_scope() as conn:
            if ports.already_queued(conn, event.email_id):
                return None
            enqueue_digest_item(
                conn,
                category="email_ambiguous",
                title=ports.translate("policy.email_triage").format(
                    subject=(event.subject or "")[:50],
                ),
                body=f"From: {event.sender_email}\nReason: {(event.reason or '')[:200]}",
                project_id=event.project_id,
                metadata={"email_id": event.email_id, "sender_email": event.sender_email},
                commit=False,
            )
            ports.record_activity(conn, event, action_type="email_queued_to_digest")
        return None

    if not ports.window_open():
        event.suppressed_reason = "outside_operator_hours"
        with ports.write_scope() as conn:
            ports.record_activity(conn, event, action_type="email_notification_suppressed")
        return None

    title_key = (
        "policy.new_email_inquiry" if classification == "new_inquiry"
        else "policy.email_triage"
    )
    notification = ports.build_notification(event, title_key=title_key)
    with ports.write_scope() as conn:
        # Notification construction may overlap another route. Only the first
        # committed marker may authorize an operator-facing card.
        if ports.already_queued(conn, event.email_id):
            return None
        if ports.group_notification is not None:
            notification = ports.group_notification(conn, event, body, notification)
        ports.record_activity(conn, event, action_type="email_operator_notification_queued")
    return notification
