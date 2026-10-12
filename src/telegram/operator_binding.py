"""Map Telegram user ids to Dashboard actors without changing card UX."""

from __future__ import annotations

from dataclasses import dataclass
import sqlite3
from typing import Any

from config import defaults
from src.auth.repository import (
    TELEGRAM_IDENTITY_PROVIDER,
    TELEGRAM_IDENTITY_TENANT,
    find_user_by_external_identity,
    normalize_telegram_user_id,
)


@dataclass(frozen=True)
class TelegramActor:
    operator_id: str
    telegram_user_id: str | None
    bound: bool


def fallback_operator_id() -> str:
    return str(getattr(defaults, "OPERATOR_ID", None) or "default_operator").strip() or "default_operator"


def telegram_user_id_from_update(update: Any) -> str | None:
    user = getattr(update, "effective_user", None) or getattr(
        getattr(update, "callback_query", None), "from_user", None
    )
    user_id = getattr(user, "id", None) if user is not None else None
    if user_id is None:
        return None
    try:
        return normalize_telegram_user_id(user_id)
    except ValueError:
        return str(user_id)


def resolve_telegram_actor(
    conn: sqlite3.Connection | None,
    telegram_user_id: str | int | None,
) -> str | None:
    """Return the active Dashboard user_id bound to *telegram_user_id*, if any.

    Unbound or disabled accounts return None so callers can keep read paths on
    CONFIG.operator_id while failing closed on high-risk writes.
    """
    if conn is None or telegram_user_id is None:
        return None
    try:
        object_id = normalize_telegram_user_id(telegram_user_id)
    except ValueError:
        return None
    user = find_user_by_external_identity(
        conn,
        provider=TELEGRAM_IDENTITY_PROVIDER,
        tenant_id=TELEGRAM_IDENTITY_TENANT,
        object_id=object_id,
    )
    if not user or user.get("status") != "active":
        return None
    return str(user["user_id"])


def resolve_telegram_operator(
    conn: sqlite3.Connection | None,
    telegram_user_id: str | int | None,
) -> TelegramActor:
    """Bound Dashboard user when known; otherwise the configured operator fallback."""
    telegram_id: str | None
    try:
        telegram_id = (
            normalize_telegram_user_id(telegram_user_id)
            if telegram_user_id is not None
            else None
        )
    except ValueError:
        telegram_id = str(telegram_user_id) if telegram_user_id is not None else None
    bound = resolve_telegram_actor(conn, telegram_id)
    if bound:
        return TelegramActor(operator_id=bound, telegram_user_id=telegram_id, bound=True)
    return TelegramActor(
        operator_id=fallback_operator_id(),
        telegram_user_id=telegram_id,
        bound=False,
    )
