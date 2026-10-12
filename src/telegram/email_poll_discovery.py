"""Classify fresh IMAP discovery; durable capture/routing stays with the poll owner."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Sequence
from typing import Any

from src.telegram.email_poll_ports import EmailPollPorts


async def classify_and_flush_discovery(
    emails: Sequence[Any],
    *,
    ports: EmailPollPorts,
    classify: Callable[..., Any],
    persist_batch_size: int,
    flush_batch: Callable[[list[tuple[Any, Any]]], Awaitable[bool]],
) -> bool:
    """Return whether classification failed; never own a DB or delivery effect.

    The caller retains capture failure state and supplies its committed-batch
    callback. Preserve discovery order, cooperative yields and abort semantics.
    """
    persist_batch: list[tuple[Any, Any]] = []
    classification_failed = False
    abort_cycle = False
    classifier = ports.build_email_classifier() if emails else None
    for idx, fetched in enumerate(emails):
        if idx > 0 and idx % ports._YIELD_EVERY == 0:
            await asyncio.sleep(0)

        # Phase 1a: Classify off event loop (LLM + read-only DB).
        try:
            event = await asyncio.to_thread(
                classify,
                None,
                fetched,
                llm_classify_fn=classifier,
            )
            persist_batch.append((fetched, event))
        except Exception:
            classification_failed = True
            ports.logger.exception(
                "Email poll: classify failed for %s ??will retry next cycle",
                fetched.email_id,
            )
            continue

        if len(persist_batch) < persist_batch_size:
            continue

        if not await flush_batch(persist_batch):
            abort_cycle = True
            break

    if not abort_cycle:
        await flush_batch(persist_batch)
    return classification_failed
