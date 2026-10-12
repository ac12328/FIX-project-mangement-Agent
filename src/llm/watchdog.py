"""
LLM watchdog — threading-based timeout guard and error card push helper.
"""

from __future__ import annotations

import concurrent.futures
import logging
import os
from typing import Any, Callable

logger = logging.getLogger(__name__)

# grok-4.6 generation (draft / inbox / summary) regularly needs 30-45s.
# 45s was cutting off healthy calls and forcing failover onto backup.
LLM_CALL_TIMEOUT_SEC = 90
# Agent reasoning / tool-calling rounds need more headroom than one-shot tasks.
LLM_REASONING_TIMEOUT_SEC = 120


class LLMTimeoutError(TimeoutError):
    """Raised when an LLM call exceeds the configured watchdog timeout."""


# Shared executor — threads are reused and capped rather than leaked.
_executor = concurrent.futures.ThreadPoolExecutor(max_workers=8, thread_name_prefix="llm-watchdog")


def call_with_timeout(fn: Callable[[], Any], timeout_sec: int = LLM_CALL_TIMEOUT_SEC) -> Any:
    """
    Run ``fn()`` in a thread pool.  If it does not return within *timeout_sec*
    seconds, raise ``LLMTimeoutError``.  Any exception raised by ``fn`` is
    re-raised in the calling thread.

    Uses a bounded ThreadPoolExecutor so threads are reused.  On timeout the
    future is cancelled (best-effort) — the thread may still run to natural
    completion but is contained in the pool rather than leaking indefinitely.

    The timeout guarantee is ALWAYS applied: previously a high queue depth
    triggered a synchronous ``fn()`` call with no timeout, which abandoned the
    watchdog's sole purpose exactly when timeouts were most likely. Submitting
    to the bounded pool naturally throttles once workers are saturated, and
    ``future.result(timeout=...)`` still raises on time even if the task has not
    started yet.
    """
    future = _executor.submit(fn)
    try:
        return future.result(timeout=timeout_sec)
    except concurrent.futures.TimeoutError:
        future.cancel()
        raise LLMTimeoutError(f"LLM call timed out after {timeout_sec}s")
    except Exception:
        # The future may still be running if fn raised; cancel best-effort so a
        # queued-but-unstarted task does not occupy a worker needlessly.
        future.cancel()
        raise


def store_retry_context(
    conn,
    *,
    project_id: str,
    trace_id: str,
    task_type: str,
    input_summary: str = "",
) -> None:
    """Store context for retry re-execution in working memory.

    Called before pushing a Profile N error card so the RETRY button can
    attempt automatic re-execution instead of asking the operator to
    re-trigger manually.
    """
    import json
    from src.repositories.working_memory_repo import upsert_memory

    ctx = json.dumps({
        "task_type": task_type,
        "project_id": project_id,
        "input_summary": input_summary,
    })
    store_id = project_id or "_system"
    # Ensure _system sentinel exists (FK on working_memory.project_id)
    if store_id == "_system":
        conn.execute(
            "INSERT OR IGNORE INTO projects (project_id, project_status, ball_with, client_name) "
            "VALUES ('_system', 'Archived', 'Internal', 'M1_NetworkSetup')",
        )
    upsert_memory(conn, store_id, f"error_retry:{trace_id}", ctx)


def _friendly_error_message(error: Exception) -> str:
    """Traditional-Chinese summary for the Profile N error card preview.

    Replaces raw English exception text (e.g. "LLM call timed out after 90s",
    "cannot schedule new futures after interpreter shutdown") so the PM does
    not see English stack traces in the card. Falls back to a short generic
    message (audit P1-10). The raw detail remains in llm_traces for engineering.
    """
    msg = str(error or "")
    cls = type(error).__name__
    lower = msg.lower()
    if isinstance(error, LLMTimeoutError) or "timed out" in lower or "timeout" in cls.lower():
        return "LLM 處理逾時，請稍後重試或使用 /status、/query。"
    if "context window" in lower or "contextwindow" in cls.lower():
        return "對話上下文過長，請縮小範圍或分拆請求後重試。"
    if "cannot schedule new futures" in msg or "interpreter shutdown" in lower:
        return "系統正在關閉，處理中斷，請稍後重試。"
    if "authentication" in lower or "unauthorized" in lower or "api key" in lower:
        return "LLM 認證失敗，請檢查 LLM 設定後重試。"
    if "rate limit" in lower or "429" in msg:
        return "LLM 已達用量上限，請稍後重試。"
    return "處理時發生錯誤，請重試；若持續出現請使用 /status、/query。"


def push_on_error(task: str, error: Exception, trace_id: str | None) -> None:
    """
    Best-effort push of a Profile N Error Card.

    Called from LLM call except-blocks.  The push must never propagate
    exceptions back to the caller — any failure is silently ignored.

    Background/optional tasks are suppressed — they run in daemon threads
    and their failure is non-critical (the operator doesn't need to know).
    """
    if os.environ.get("AGENTFIXB_DISABLE_LLM_ERROR_CARDS") == "1":
        logger.debug("Suppressing LLM error card because runtime notifications are disabled")
        return

    # Suppress error cards for background enrichment tasks
    _SILENT_TASKS = {
        "summary_update", "compression", "flush", "session_summary", "image_description",
    }
    if task in _SILENT_TASKS:
        logger.debug("Suppressing error card for background task %s: %s", task, error)
        return

    try:
        from src.observability.traces import classify_error
        from src.telegram.card_push import push_error_card_sync

        # The card adapter owns its short receipt/budget scopes. Never hold the
        # shared writer lease while its Telegram send can block or be uncertain.
        push_error_card_sync(
            None,
            task=task,
            error_class=classify_error(error),
            trace_id=trace_id,
            error_message=_friendly_error_message(error),
        )
    except Exception:
        pass
