"""
Thread-safe notification queue for proactive agent awareness (P8).
Notifications are drained in priority order (highest score first).
"""
from __future__ import annotations

import heapq
import threading
from datetime import datetime, timezone

_notification_queue: list[tuple[int, int, dict]] = []
_notification_lock = threading.Lock()
_seq_counter = 0


def enqueue_notification(
    notification_type: str,
    summary: str,
    project_id: str | None = None,
    score: int = 0,
) -> None:
    """Push a notification into the priority queue. Higher score = higher priority."""
    global _seq_counter
    with _notification_lock:
        _seq_counter += 1
        item = {
            "type": notification_type,
            "summary": summary,
            "project_id": project_id,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "score": score,
        }
        heapq.heappush(_notification_queue, (-score, _seq_counter, item))


def drain_notifications() -> str | None:
    """Drain all pending notifications in priority order. Returns formatted string or None."""
    with _notification_lock:
        if not _notification_queue:
            return None
        items = []
        while _notification_queue:
            _, _, item = heapq.heappop(_notification_queue)
            items.append(item)

    lines = []
    for n in items:
        pid = f" [{n['project_id']}]" if n.get("project_id") else ""
        lines.append(f"- [{n['type']}]{pid} {n['summary']}")
    return "\n".join(lines)


def pending_count() -> int:
    with _notification_lock:
        return len(_notification_queue)


def clear_all() -> None:
    global _seq_counter
    with _notification_lock:
        _notification_queue.clear()
        _seq_counter = 0
