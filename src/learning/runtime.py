"""Runtime switches for optional learning persistence."""

from __future__ import annotations

import os

_TRUTHY = frozenset({"1", "true", "yes", "on"})


def learning_writes_disabled() -> bool:
    """Return whether optional learning/lineage writes are disabled for this process."""
    return os.getenv("AGENTFIXB_DISABLE_LEARNING_WRITES", "").strip().casefold() in _TRUTHY


__all__ = ["learning_writes_disabled"]
