"""Bounded thread-aware memory for consecutive typed portfolio reads.

The compact planner may need the last validated query to resolve phrases such
as "those projects". The cache is process-local, scoped by surface,
conversation, and the exact trusted project scope. It stores only validated
typed query payloads and never changes business data.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class _QueryMemoryEntry:
    scope_mode: str
    project_ids: tuple[str, ...]
    query: dict[str, Any]
    recorded_at: float


class ToolCentricQueryMemory:
    """Small TTL/LRU-style cache isolated by conversation and trusted scope."""

    def __init__(self, *, capacity: int = 1024, ttl_seconds: float = 3600.0) -> None:
        self._capacity = max(1, int(capacity))
        self._ttl_seconds = max(1.0, float(ttl_seconds))
        self._entries: dict[tuple[str, str], _QueryMemoryEntry] = {}
        self._lock = threading.Lock()

    def get(
        self,
        *,
        surface: str,
        conversation_id: str | None,
        scope_mode: str,
        project_ids: tuple[str, ...],
    ) -> dict[str, Any] | None:
        key = self._key(surface, conversation_id)
        if key is None:
            return None
        now = time.monotonic()
        with self._lock:
            entry = self._entries.get(key)
            if entry is None:
                return None
            if now - entry.recorded_at > self._ttl_seconds:
                self._entries.pop(key, None)
                return None
            if entry.scope_mode != scope_mode or entry.project_ids != project_ids:
                return None
            return dict(entry.query)

    def remember(
        self,
        *,
        surface: str,
        conversation_id: str | None,
        scope_mode: str,
        project_ids: tuple[str, ...],
        query: dict[str, Any],
    ) -> None:
        key = self._key(surface, conversation_id)
        if key is None:
            return
        now = time.monotonic()
        with self._lock:
            self._prune_expired(now)
            if len(self._entries) >= self._capacity and key not in self._entries:
                oldest = min(
                    self._entries,
                    key=lambda item: self._entries[item].recorded_at,
                )
                self._entries.pop(oldest, None)
            self._entries[key] = _QueryMemoryEntry(
                scope_mode=scope_mode,
                project_ids=project_ids,
                query=dict(query),
                recorded_at=now,
            )

    def clear(self) -> None:
        with self._lock:
            self._entries.clear()

    def _prune_expired(self, now: float) -> None:
        expired = [
            key
            for key, entry in self._entries.items()
            if now - entry.recorded_at > self._ttl_seconds
        ]
        for key in expired:
            self._entries.pop(key, None)

    @staticmethod
    def _key(surface: str, conversation_id: str | None) -> tuple[str, str] | None:
        normalized_surface = str(surface or "").strip().lower()
        normalized_conversation = str(conversation_id or "").strip()
        if not normalized_surface or not normalized_conversation:
            return None
        return normalized_surface, normalized_conversation


_QUERY_MEMORY = ToolCentricQueryMemory()


def get_previous_tool_centric_query(
    *,
    surface: str,
    conversation_id: str | None,
    scope_mode: str,
    project_ids: tuple[str, ...],
) -> dict[str, Any] | None:
    return _QUERY_MEMORY.get(
        surface=surface,
        conversation_id=conversation_id,
        scope_mode=scope_mode,
        project_ids=project_ids,
    )


def remember_tool_centric_query(
    *,
    surface: str,
    conversation_id: str | None,
    scope_mode: str,
    project_ids: tuple[str, ...],
    query: dict[str, Any],
) -> None:
    _QUERY_MEMORY.remember(
        surface=surface,
        conversation_id=conversation_id,
        scope_mode=scope_mode,
        project_ids=project_ids,
        query=query,
    )


def reset_tool_centric_query_memory() -> None:
    """Clear process-local state for tests and controlled simulations."""
    _QUERY_MEMORY.clear()


__all__ = [
    "ToolCentricQueryMemory",
    "get_previous_tool_centric_query",
    "remember_tool_centric_query",
    "reset_tool_centric_query_memory",
]
