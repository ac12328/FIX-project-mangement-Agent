"""Soft write policy for memory stores.

Guides writers toward the correct store without hard-blocking.
See docs/domain/MEMORY_OWNERSHIP.md.
"""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)

# Keys that look like operator preferences (should use operator_memory).
PREF_PREFIXES = ("pref_", "preference_", "forbidden_", "avoid_")


def warn_if_preference_in_working_memory(memory_key: str) -> None:
    """Log warning if a preference-like key is written to working_memory."""
    if any(memory_key.startswith(p) or memory_key == p.rstrip("_") for p in PREF_PREFIXES):
        logger.warning(
            "working_memory key %r looks like operator preference; prefer operator_memory",
            memory_key,
        )
