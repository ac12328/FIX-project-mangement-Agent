"""Telegram Rich Message API helpers (Bot API 10.1+).

python-telegram-bot may not yet expose sendRichMessage* wrappers.
Use native methods when present, otherwise Bot.do_api_request.
"""

from __future__ import annotations

import inspect
import logging
from typing import Any

logger = logging.getLogger(__name__)


async def _await_maybe(result: Any) -> Any:
    if inspect.isawaitable(result):
        return await result
    return result


def _callable_attr(bot: Any, name: str) -> Any | None:
    """Resolve a bot method without treating auto MagicMock children as real APIs."""
    inst = getattr(bot, "__dict__", None)
    if isinstance(inst, dict) and name in inst:
        method = inst[name]
        return method if callable(method) else None

    # Real library methods live on the class (e.g. telegram.Bot.do_api_request).
    cls = type(bot)
    if getattr(cls, "__module__", "").startswith("unittest.mock"):
        return None
    cls_attr = getattr(cls, name, None)
    if callable(cls_attr):
        return getattr(bot, name)
    return None


def _draft_ok(result: Any) -> bool:
    """Draft endpoints return True on success (reject bare mocks)."""
    return result is True


def extract_message_id(result: Any) -> int | None:
    mid: Any = None
    if isinstance(result, dict):
        mid = result.get("message_id")
        if mid is None and isinstance(result.get("result"), dict):
            mid = result["result"].get("message_id")
    else:
        mid = getattr(result, "message_id", None)
    if mid is None:
        return None
    # Reject MagicMock / non-int ids that coerce poorly.
    if type(mid) is bool or not isinstance(mid, (int, str)):
        return None
    try:
        value = int(mid)
    except (TypeError, ValueError):
        return None
    if value <= 0:
        return None
    return value


async def send_rich_message_draft(
    bot: Any,
    *,
    chat_id: int,
    draft_id: int,
    markdown: str | None = None,
    html: str | None = None,
    message_thread_id: int | None = None,
) -> bool:
    """Stream a partial rich message (ephemeral ~30s). Private chats only."""
    rich_message: dict[str, Any]
    if markdown is not None and markdown != "":
        rich_message = {"markdown": markdown}
    elif html is not None:
        rich_message = {"html": html}
    else:
        rich_message = {"html": "<tg-thinking>Thinking...</tg-thinking>"}

    kwargs: dict[str, Any] = {
        "chat_id": chat_id,
        "draft_id": draft_id,
        "rich_message": rich_message,
    }
    if message_thread_id is not None:
        kwargs["message_thread_id"] = message_thread_id

    method = _callable_attr(bot, "send_rich_message_draft")
    if method is not None:
        result = await _await_maybe(method(**kwargs))
        return _draft_ok(result)

    do_api = _callable_attr(bot, "do_api_request")
    if do_api is None:
        return False
    result = await _await_maybe(do_api("sendRichMessageDraft", api_kwargs=kwargs))
    return _draft_ok(result)


async def send_rich_message(
    bot: Any,
    *,
    chat_id: int,
    markdown: str,
    reply_to_message_id: int | None = None,
    message_thread_id: int | None = None,
    disable_notification: bool | None = None,
) -> Any | None:
    """Persist a rich message. Returns API result (Message-like) or None."""
    rich_message = {"markdown": markdown}
    kwargs: dict[str, Any] = {
        "chat_id": chat_id,
        "rich_message": rich_message,
    }
    if message_thread_id is not None:
        kwargs["message_thread_id"] = message_thread_id
    if reply_to_message_id is not None:
        kwargs["reply_parameters"] = {"message_id": reply_to_message_id}
    if disable_notification is not None:
        kwargs["disable_notification"] = disable_notification

    try:
        method = _callable_attr(bot, "send_rich_message")
        if method is not None:
            result = await _await_maybe(method(**kwargs))
        else:
            do_api = _callable_attr(bot, "do_api_request")
            if do_api is None:
                logger.warning("sendRichMessage unavailable (no do_api_request)")
                return None
            result = await _await_maybe(do_api("sendRichMessage", api_kwargs=kwargs))
    except Exception:
        logger.warning("sendRichMessage failed", exc_info=True)
        return None

    if result is None or result is False:
        logger.warning("sendRichMessage returned empty result")
        return None
    # Require a real integer message_id (rejects bare MagicMock returns in tests).
    if extract_message_id(result) is None:
        logger.warning("sendRichMessage result missing message_id: %r", type(result))
        return None
    return result


def bot_can_attempt_rich(bot: Any) -> bool:
    """True when the bot object can attempt rich endpoints."""
    return (
        _callable_attr(bot, "send_rich_message") is not None
        or _callable_attr(bot, "send_rich_message_draft") is not None
        or _callable_attr(bot, "do_api_request") is not None
    )
