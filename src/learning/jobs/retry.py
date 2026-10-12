"""Deterministic bounded retry policy for learning jobs."""
from __future__ import annotations


def retry_delay_seconds(attempts: int, *, base_seconds: int = 60, max_seconds: int = 3600) -> int:
    """Return exponential delay without jitter so retries are reproducible."""
    return min(max_seconds, base_seconds * (2 ** max(0, attempts - 1)))
