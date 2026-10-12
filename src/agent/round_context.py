"""Per-round context injection beyond static system prompts."""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import datetime
from typing import Protocol, runtime_checkable

from src.repositories.project_repo import get_project


@runtime_checkable
class RoundContextProvider(Protocol):
    def poll(
        self,
        *,
        round_num: int,
        conn: sqlite3.Connection | None,
        effective_project_id: str | None,
        loop_start_utc: datetime | None,
    ) -> str | None:
        """Return optional XML body for <round-context>."""


@dataclass
class CompositeRoundContext:
    providers: list[RoundContextProvider]

    def poll(self, **kwargs) -> str | None:
        parts = [p.poll(**kwargs) for p in self.providers]
        body = "\n".join(p for p in parts if p)
        return body or None


class TelegramNotificationsProvider:
    """Drain Telegram notification queue."""

    def __init__(self, drain_fn):
        self._drain = drain_fn

    def poll(self, **kwargs) -> str | None:
        return self._drain()


class DbDriftProvider:
    """Signal when project row changed after loop start."""

    def poll(
        self,
        *,
        round_num: int,
        conn: sqlite3.Connection | None,
        effective_project_id: str | None,
        loop_start_utc: datetime | None,
        **_,
    ) -> str | None:
        if not effective_project_id or loop_start_utc is None:
            return None
        if round_num == 0:
            return None
        if conn is None:
            from src.db.connection import scoped_read

            with scoped_read() as rconn:
                return self.poll(
                    round_num=round_num,
                    conn=rconn,
                    effective_project_id=effective_project_id,
                    loop_start_utc=loop_start_utc,
                )
        project = get_project(conn, effective_project_id)
        if project is None or not project.updated_at:
            return None
        updated = project.updated_at
        if updated.tzinfo is None:
            from datetime import timezone
            updated = updated.replace(tzinfo=timezone.utc)
        if updated > loop_start_utc:
            return (
                f'<db-drift project_id="{effective_project_id}" '
                f'updated_at="{updated.isoformat()}" />'
            )
        return None


def wrap_pre_round_hook(hook) -> RoundContextProvider:
    """Backward-compatible adapter for legacy pre_round_hook callables."""

    class _LegacyHookProvider:
        def poll(self, **kwargs) -> str | None:
            if hook is None:
                return None
            return hook()

    return _LegacyHookProvider()