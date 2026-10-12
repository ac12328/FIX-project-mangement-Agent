"""A source-bound field decision nested inside a durable inbound reply."""
from __future__ import annotations

import logging
from collections.abc import Callable
from typing import Any

from src.db.borrowed_transaction import BorrowedTransactionConnection
from src.db.savepoint import savepoint
from src.email.update_outcomes import COMMITTED_UPDATE_OUTCOMES

logger = logging.getLogger(__name__)

_NONPERSISTENT = frozenset({
    "skipped", "below_review_threshold", "assessment_required",
    "review_persistence_failed",
})
_PERSISTENT = COMMITTED_UPDATE_OUTCOMES | frozenset({
    "review_card", "review_notification_pending", "review_notification_unknown",
})


class _NoFieldDecision(Exception):
    def __init__(self, outcome: str):
        self.outcome = outcome
        super().__init__(outcome)


def route_reply_fields(
    conn: Any, *, post_commit: list[Callable[[], None]], **route_kwargs: Any,
) -> str:
    """Leave the recorded reply intact; discard partial field effects and callbacks.

    The caller owns the surrounding reply and checkpoint commit. A route error
    produces an explicit incomplete receipt for reconciliation, not a successful
    reply card. Ordinary skipped/idempotent outcomes have no field effects.
    """
    from src.email.reply_analyser import route_proposed_field_updates

    staged: list[Callable[[], None]] = []
    try:
        with savepoint(conn, ensure_transaction=True):
            outcome = route_proposed_field_updates(
                BorrowedTransactionConnection(conn), post_commit=staged,
                raise_on_error=True, **route_kwargs,
            )
            if outcome in _NONPERSISTENT:
                raise _NoFieldDecision(outcome)
            if outcome not in _PERSISTENT:
                raise ValueError(f"Unknown inbound reply field outcome: {outcome!r}")
    except _NoFieldDecision as skipped:
        return skipped.outcome
    except Exception:
        logger.exception("Inbound reply field command failed for %s", route_kwargs.get("project_id"))
        return "field_route_failed"
    post_commit.extend(staged)
    return outcome
