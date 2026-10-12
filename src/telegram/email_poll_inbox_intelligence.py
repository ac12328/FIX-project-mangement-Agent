"""Lease-bound inbox insight and deferred digest processing for IMAP routes."""
from __future__ import annotations

import asyncio

from src.email.inbound_work import checkpoint_route_step


def _checkpoint(conn, email_id: str, token: str, status: str) -> None:
    if not checkpoint_route_step(
        conn, email_id, token, step="inbox_intelligence", status=status,
    ):
        raise RuntimeError(f"Inbound inbox_intelligence checkpoint lost for {email_id}")


async def route_inbox_intelligence(
    fetched, event, token: str, *, scoped_write, logger,
) -> bool:
    """Keep committed insight work and its digest covered by a route receipt."""
    from src.auth.principals import SHARED_MAILBOX_PRINCIPAL
    from src.email.inbox_intelligence import analyze_inbox_email
    from src.repositories.digest_queue_repo import enqueue_digest_item

    with scoped_write() as conn:
        _checkpoint(conn, fetched.email_id, token, "started")
    try:
        insight = await asyncio.to_thread(
            analyze_inbox_email, None, email_id=fetched.email_id,
            operator_id=SHARED_MAILBOX_PRINCIPAL, strict_work_item=True,
        )
        with scoped_write() as conn:
            if insight.suppressed:
                enqueue_digest_item(
                    conn, category="email_deferred",
                    title=f"📧 {(fetched.subject or '')[:50]}",
                    body=(insight.request_summary or "No action required")[:500],
                    project_id=event.project_id,
                    metadata={"insight_id": insight.insight_id, "action_required": False},
                    commit=False,
                )
            _checkpoint(conn, fetched.email_id, token, "completed")
    except Exception:
        # An insight/work-item commit can precede a missing route receipt.
        # Do not replay this work on a later poll without reconciliation.
        with scoped_write() as conn:
            _checkpoint(conn, fetched.email_id, token, "incomplete")
        logger.warning("Inbox intelligence incomplete for %s", fetched.email_id,
                       exc_info=True)
        return False
    return True
