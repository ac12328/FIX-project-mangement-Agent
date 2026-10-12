"""Compatibility facade for the surface-neutral notification queue."""

from src.events.notification_queue import (
    clear_all,
    drain_notifications,
    enqueue_notification,
    pending_count,
)

__all__ = [
    "clear_all",
    "drain_notifications",
    "enqueue_notification",
    "pending_count",
]
