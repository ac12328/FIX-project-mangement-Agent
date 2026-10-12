"""Telegram routing and presentation metadata for deterministic closing commands.

This module selects existing domain executors and supplies Telegram presentation
metadata; transaction ownership stays with those executors. In particular,
sign-off remains Dashboard-only guidance, never a Telegram closing write.
"""

from __future__ import annotations

import sqlite3
from typing import Literal

from pydantic import BaseModel, ConfigDict


CommandName = Literal["go_live", "sign_off"]


class DeterministicCommand(BaseModel):
    """A routed executor reply and its Telegram presentation contract."""

    model_config = ConfigDict(frozen=True)

    reply: str
    project_id: str | None
    name: CommandName

    @property
    def context_progress(self) -> str:
        return "Deterministic sign-off" if self.name == "sign_off" else "Deterministic go-live"

    @property
    def round_progress(self) -> str:
        return "Closing review available" if self.name == "sign_off" else "Go-live staged"

    @property
    def exit_reason(self) -> str:
        return f"deterministic_{self.name}"


def _try_sign_off(
    conn: sqlite3.Connection | None, user_text: str, chat_id: int,
) -> DeterministicCommand | None:
    from src.agent.sign_off_executor import try_deterministic_sign_off

    advisory = try_deterministic_sign_off(conn, user_text, chat_id)
    if advisory is None:
        return None
    reply, project_id = advisory
    return DeterministicCommand(reply=reply, project_id=project_id, name="sign_off")


def _try_go_live(
    conn: sqlite3.Connection | None, user_text: str, chat_id: int,
) -> DeterministicCommand | None:
    from src.agent.go_live_executor import try_deterministic_go_live

    staged = try_deterministic_go_live(conn, user_text, chat_id)
    if staged is None:
        return None
    reply, project_id = staged
    return DeterministicCommand(reply=reply, project_id=project_id, name="go_live")


def try_early_sign_off(
    conn: sqlite3.Connection | None, user_text: str, chat_id: int,
) -> DeterministicCommand | None:
    """Intercept explicit closing before reconciliation or either model path."""
    from src.agent.guards import is_production_sign_off

    if is_production_sign_off(user_text):
        return _try_sign_off(conn, user_text, chat_id)
    return None


def try_post_context_command(
    conn: sqlite3.Connection | None, user_text: str, chat_id: int,
) -> DeterministicCommand | None:
    """Prefer go-live for pure updates, closing advisory for dual intents.

    When closing cannot resolve a project, a dual request may still stage a
    go-live update. This is the original late-route fallback after the early
    sign-off check and unified-v2 path have both declined the request.
    """
    from src.agent.go_live_executor import is_go_live_update_intent
    from src.agent.guards import is_production_sign_off

    want_go_live = is_go_live_update_intent(user_text)
    want_close = is_production_sign_off(user_text)
    if want_go_live and not want_close:
        staged = _try_go_live(conn, user_text, chat_id)
        if staged is not None:
            return staged
    if want_close:
        advisory = _try_sign_off(conn, user_text, chat_id)
        if advisory is not None:
            return advisory
    if want_go_live:
        return _try_go_live(conn, user_text, chat_id)
    return None
