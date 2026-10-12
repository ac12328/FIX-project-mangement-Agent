"""
In-memory event bus for cross-surface notifications.

Telegram callbacks and Dashboard API both publish events here.
SSE clients subscribe and receive live updates.
"""

from __future__ import annotations

import asyncio
import functools
import json
import threading
import time
from dataclasses import dataclass, field, asdict
from typing import AsyncIterator


@dataclass
class Event:
    """A single event to be broadcast to all subscribers."""
    event_type: str          # e.g. "review_approved", "draft_discarded"
    project_id: str | None = None
    detail: dict = field(default_factory=dict)
    surface: str = "system"  # "telegram" | "dashboard" | "system"
    timestamp: float = field(default_factory=time.time)

    def to_sse(self) -> str:
        payload = json.dumps(asdict(self), default=str)
        return f"event: {self.event_type}\ndata: {payload}\n\n"


def _safe_put(q: asyncio.Queue, event: Event) -> None:
    """Put an event into a queue, swallowing QueueFull. Called on the event loop."""
    try:
        q.put_nowait(event)
    except asyncio.QueueFull:
        pass


class EventBus:
    """Thread-safe fan-out pub/sub using asyncio queues.

    Each SSE client gets its own queue. Publishing pushes
    the event into every active queue via ``call_soon_threadsafe``
    so it is safe to call from worker threads (FastAPI sync endpoints,
    Telegram callback threads, daemon background threads).
    """

    def __init__(self) -> None:
        # A process can serve subscribers on different event loops over time
        # (or concurrently). Keep each queue bound to its own running loop.
        self._subscribers: list[tuple[asyncio.AbstractEventLoop, asyncio.Queue[Event]]] = []
        self._lock = threading.Lock()

    def publish(self, event: Event) -> None:
        """Push event to current subscribers without touching queues across loops."""
        with self._lock:
            subscribers = list(self._subscribers)

        for loop, queue in subscribers:
            try:
                loop.call_soon_threadsafe(functools.partial(_safe_put, queue, event))
            except RuntimeError:
                if not loop.is_closed():
                    raise
                # A loop can close between snapshot and scheduling, or without
                # its async generator being finalized. Retire only that entry.
                with self._lock:
                    try:
                        self._subscribers.remove((loop, queue))
                    except ValueError:
                        pass

    async def subscribe(self, *, max_queue: int = 256) -> AsyncIterator[Event]:
        """Yield events as they arrive. Use in an async for loop."""
        queue: asyncio.Queue[Event] = asyncio.Queue(maxsize=max_queue)
        subscriber = (asyncio.get_running_loop(), queue)
        with self._lock:
            self._subscribers.append(subscriber)
        try:
            while True:
                event = await queue.get()
                yield event
        finally:
            with self._lock:
                try:
                    self._subscribers.remove(subscriber)
                except ValueError:
                    pass

    @property
    def subscriber_count(self) -> int:
        with self._lock:
            return len(self._subscribers)


# Module-level singleton — imported by both API and Telegram
event_bus = EventBus()
