"""In-process counters for Workspace operator updates.

Do not extend project_state observability — that event set is closed.
"""

from __future__ import annotations

from collections import Counter
from threading import RLock

_LOCK = RLock()
_COUNTS: Counter[str] = Counter()


def increment(name: str, **labels: str) -> None:
    parts = [name]
    for key in sorted(labels):
        parts.append(f"{key}={labels[key]}")
    with _LOCK:
        _COUNTS[" ".join(parts)] += 1


def snapshot() -> dict[str, int]:
    with _LOCK:
        return dict(_COUNTS)


def reset() -> None:
    with _LOCK:
        _COUNTS.clear()
