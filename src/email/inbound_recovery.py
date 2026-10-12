"""Recover never-started IMAP work independently from UID discovery."""
from __future__ import annotations

import asyncio
import json
from typing import Awaitable, Callable

from src.email.inbox_fetcher import SourceUnavailable
from src.email.source_capture import SourceCaptureReceipt, fetched_matches_capture

from src.email.inbound_work import (
    ImapSource, RouteClaimExpectation, claim_pending_route, expire_routing_leases,
    list_pending_imap, mark_pending_source_retry,
    require_reconciliation,
)
from src.triage.email_triage import ClassifiedEvent


async def recover_pending_imap(
    fetcher, *, scoped_read, scoped_write, process: Callable[..., Awaitable[None]],
    logger, limit: int,
) -> None:
    """Only pending (never-started) work is replayable; claims are fail-closed."""
    with scoped_write() as conn:
        expired = expire_routing_leases(conn, source_type="imap")
    if expired:
        logger.error("IMAP recovery: %d expired route leases need reconciliation", expired)
    account_key = ImapSource.from_fetcher(fetcher, 1).account_key
    with scoped_read() as conn:
        rows = list_pending_imap(conn, account_key, fetcher.inbox_folder, limit=limit)
    for row in rows:
        # Re-fetch cannot have caused business effects. Leave never-started work
        # pending if the transport fails; claim only after source verification.
        try:
            fetched = await asyncio.to_thread(
                fetcher.refetch_inbox_source,
                uid=row["source_uid"], uid_validity=row["uid_validity"],
                email_id=row["email_id"],
            )
        except SourceUnavailable as exc:
            _reconcile_pending(scoped_write, row["email_id"], type(exc).__name__)
            logger.exception("IMAP recovery source needs reconciliation for %s", row["email_id"])
            continue
        except Exception:
            with scoped_write() as conn:
                mark_pending_source_retry(conn, row["email_id"], source_type="imap")
            logger.warning("IMAP recovery will retry source %s next poll", row["email_id"],
                           exc_info=True)
            continue
        try:
            event = ClassifiedEvent(**json.loads(row["classification_json"]))
            capture = json.loads(row["checkpoint_json"]).get("source_capture")
            if capture is not None and not fetched_matches_capture(
                fetched, ImapSource(row["account_key"], row["mailbox"], row["uid_validity"]),
                SourceCaptureReceipt.model_validate(capture),
            ):
                raise ValueError("Recovered source differs from the retained original")
            if (row["anchor_email_id"] != row["email_id"] or
                event.email_id != row["email_id"] or
                event.sender_email != row["anchor_sender_email"] or
                event.subject != row["anchor_subject"] or
                event.classification_type != row["anchor_result"] or
                event.project_id != row["anchor_project_id"] or
                fetched.sender_email != row["anchor_sender_email"] or
                fetched.subject != row["anchor_subject"]
            ):
                raise ValueError("IMAP classification or source differs from captured anchor")
        except Exception as exc:
            _reconcile_pending(scoped_write, row["email_id"], type(exc).__name__)
            logger.exception("IMAP recovery classification needs reconciliation for %s", row["email_id"])
            continue
        with scoped_write() as conn:
            token = claim_pending_route(
                conn, row["email_id"], RouteClaimExpectation.from_pending(row, event),
            )
        if token is None:
            _reconcile_pending(scoped_write, row["email_id"], "claim snapshot changed")
            continue
        try:
            await process(fetched, event, True, route_token=token)
        except Exception as exc:
            with scoped_write() as conn:
                require_reconciliation(conn, row["email_id"], token,
                                       reason=f"recovery failure: {type(exc).__name__}")
            logger.exception("IMAP recovery needs reconciliation for %s", row["email_id"])


def _reconcile_pending(scoped_write, email_id: str, failure: str) -> None:
    """Quarantine a permanently unverifiable pending source without rerouting."""
    with scoped_write() as conn:
        token = claim_pending_route(conn, email_id)
        if token is not None:
            require_reconciliation(conn, email_id, token,
                                   reason=f"recovery failure: {failure}")
