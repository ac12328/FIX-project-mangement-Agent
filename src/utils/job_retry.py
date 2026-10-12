"""Async retry helper for scheduler jobs."""

import asyncio
import logging

logger = logging.getLogger(__name__)

NON_RETRYABLE_PATTERNS = (
    "ContextWindowExceededError",
    "InvalidRequestError",
    "AuthenticationError",
)


def _is_non_retryable(
    exc: Exception,
    patterns: tuple[str, ...] = NON_RETRYABLE_PATTERNS,
) -> bool:
    """Return True if the exception is deterministic and should not be retried."""
    exc_type = type(exc).__name__
    exc_str = str(exc)
    return any(p in exc_type or p in exc_str for p in patterns)


async def with_async_retry(
    fn,
    *,
    max_retries: int = 1,
    delay_seconds: float = 60,
    job_name: str = "",
    non_retryable: tuple[str, ...] = NON_RETRYABLE_PATTERNS,
):
    """Call an async function, retrying up to max_retries times on exception.

    Deterministic errors (matching non_retryable patterns) raise immediately
    without sleeping or retrying.

    Returns the result on success. Raises the last exception on exhaustion.
    """
    last_exc = None
    for attempt in range(1 + max_retries):
        try:
            return await fn()
        except Exception as exc:
            last_exc = exc

            if _is_non_retryable(exc, non_retryable):
                logger.error(
                    "Job %s failed with non-retryable error (attempt %d), skipping retry: %s",
                    job_name, attempt + 1, exc,
                )
                raise

            if attempt < max_retries:
                logger.warning(
                    "Job %s failed (attempt %d/%d), retrying in %.0fs: %s",
                    job_name, attempt + 1, 1 + max_retries, delay_seconds, exc,
                )
                await asyncio.sleep(delay_seconds)
            else:
                logger.error(
                    "Job %s failed after %d attempts: %s",
                    job_name, 1 + max_retries, exc,
                )
    raise last_exc
