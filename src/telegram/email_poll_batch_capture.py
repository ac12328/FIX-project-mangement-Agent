"""Batch capture outcome for scheduled IMAP polling; delivery remains in the caller."""

from __future__ import annotations

import sqlite3
from contextlib import AbstractContextManager
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Callable

if TYPE_CHECKING:
    from src.email.inbox_fetcher import FetchedEmail
    from src.email.inbound_work import ImapSource
    from src.triage.email_triage import ClassifiedEvent


LOCK_FAILURE_CIRCUIT_BREAKER = 3


@dataclass(frozen=True)
class CaptureBatchResult:
    persisted: list[tuple[FetchedEmail, ClassifiedEvent, bool]] | None
    consecutive_lock_failures: int
    failed: bool
    abort_cycle: bool


def capture_poll_batch(
    batch: list[tuple[FetchedEmail, ClassifiedEvent]],
    *,
    source: ImapSource,
    scoped_write: Callable[[], AbstractContextManager[sqlite3.Connection]],
    capture: Callable[..., list[tuple[FetchedEmail, ClassifiedEvent, bool]]],
    logger: Any,
    consecutive_lock_failures: int,
) -> CaptureBatchResult:
    """Capture only committed work; never hand failed captures to the router."""
    try:
        with scoped_write() as conn:
            persisted = capture(conn, batch, source=source)
    except Exception as exc:
        if "database is locked" in str(exc):
            consecutive_lock_failures += 1
            if consecutive_lock_failures >= LOCK_FAILURE_CIRCUIT_BREAKER:
                logger.error(
                    "Email poll: %d consecutive 'database is locked' errors -- "
                    "aborting cycle to avoid blocking scheduler. "
                    "Remaining emails will retry next cycle.",
                    consecutive_lock_failures,
                )
                batch.clear()
                return CaptureBatchResult(None, consecutive_lock_failures, True, True)
        logger.exception(
            "Email poll: batch persist failed (%d emails) -- will retry next cycle",
            len(batch),
        )
        batch.clear()
        return CaptureBatchResult(None, consecutive_lock_failures, True, False)

    batch.clear()
    return CaptureBatchResult(persisted, 0, False, False)
