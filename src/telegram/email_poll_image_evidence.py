"""Retain exact per-image outputs without certifying an incomplete source batch."""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone

from src.email.inbound_work import checkpoint_route_step
from src.email.image_result_receipts import (
    batch_context, record_image_result, validated_result, validate_batch_completion,
)


def _checkpoint(conn, email_id: str, token: str, status: str) -> None:
    if not checkpoint_route_step(conn, email_id, token, step="image_evidence", status=status):
        raise RuntimeError(f"Inbound image_evidence checkpoint lost for {email_id}")


async def route_image_evidence(fetched, event, token: str, *, scoped_write, logger) -> bool:
    """Complete selected evidence only when coverage guards pass; never retry outputs."""
    images = [item for item in fetched.attachments if item.content_type.startswith("image/")]
    coverage = fetched.source_coverage
    known_gap = coverage is not None and not coverage.image_selection_complete
    if not event.project_id or (not images and not known_gap):
        return True

    from src.email.email_processor import process_image_attachments
    from src.repositories.evidence_repo import insert_evidence

    with scoped_write() as conn:
        _checkpoint(conn, fetched.email_id, token, "started")
    try:
        with scoped_write() as conn:
            context = batch_context(conn, fetched, event.project_id, token)

        def retain_result(result):
            with scoped_write() as conn:
                record_image_result(conn, token, context, result)

        descriptions = await asyncio.to_thread(
            process_image_attachments, None, fetched, event.project_id, on_result=retain_result,
        ) if images else []
        receipts = []
        invalid = False
        for result in descriptions:
            try:
                retain_result(result)  # Idempotent for outputs already retained incrementally.
                receipts.append(validated_result(context, result))
            except (ValueError, KeyError, TypeError):
                invalid = True
        if (invalid or len(receipts) != len(images)
                or len({receipt.part_id for receipt in receipts}) != len(images)
                or len({receipt.path for receipt in receipts}) != len(images)
                or known_gap or context.original_selection_complete is False):
            raise ValueError("Image source/description coverage is incomplete")
        # Partial outputs above remain outside downstream evidence. All evidence
        # inserts and the full batch checkpoint still commit/roll back together.
        with scoped_write() as conn:
            validate_batch_completion(conn, token, context, receipts)
            now = datetime.now(timezone.utc).isoformat()
            for receipt in receipts:
                insert_evidence(
                    conn, project_id=event.project_id, evidence_type="attachment",
                    content_summary=receipt.description, evidence_at=now,
                    content_text=receipt.description,
                    source_ref=f"attachment:{fetched.email_id}:{receipt.path}",
                )
            _checkpoint(conn, fetched.email_id, token, "completed")
    except Exception:
        # Missing local receipts do not prove absence of external effects.
        # Lease loss must not overwrite another owner's work or cause replay.
        with scoped_write() as conn:
            checkpoint_route_step(conn, fetched.email_id, token, step="image_evidence", status="incomplete")
        logger.warning("Image attachment processing incomplete for %s", fetched.email_id, exc_info=True)
        return False
    return True
