"""
Push guard — determines whether a proactive Telegram notification should be sent
based on the operator's current notification_mode setting and optional attention score.

Tracks suppressed notifications so a digest can be shown later.
"""
from __future__ import annotations

import threading

URGENCY_NORMAL = "normal"
URGENCY_URGENT = "urgent"
URGENCY_EMERGENCY = "emergency"

SCORE_PROMOTE_THRESHOLD = 50

_ALLOW_MATRIX: dict[str, set[str]] = {
    "active":    {URGENCY_NORMAL, URGENCY_URGENT, URGENCY_EMERGENCY},
    "silent":    {URGENCY_URGENT, URGENCY_EMERGENCY},
    "emergency": {URGENCY_EMERGENCY},
}

# Thread-safe suppressed notification counter
_suppressed_lock = threading.Lock()
_suppressed_count: int = 0


def should_push(mode: str, urgency: str, score: int | None = None) -> bool:
    """Return True if a notification should be sent.

    When score >= SCORE_PROMOTE_THRESHOLD, normal urgency is promoted to urgent.
    Increments the suppressed counter when a notification is blocked by mode.
    """
    global _suppressed_count
    effective_urgency = urgency
    if score is not None and score >= SCORE_PROMOTE_THRESHOLD:
        if effective_urgency == URGENCY_NORMAL:
            effective_urgency = URGENCY_URGENT
    allowed = _ALLOW_MATRIX.get(mode, _ALLOW_MATRIX["active"])
    if effective_urgency in allowed:
        return True
    with _suppressed_lock:
        _suppressed_count += 1
    return False


def get_suppressed_count() -> int:
    """Return the number of notifications suppressed since last reset."""
    with _suppressed_lock:
        return _suppressed_count


def reset_suppressed_count() -> int:
    """Reset the counter and return the value before reset."""
    global _suppressed_count
    with _suppressed_lock:
        count = _suppressed_count
        _suppressed_count = 0
    return count
