"""Unified Telegram text delivery: Rich Markdown → HTML → plain.

Used by send_persisted_message, scheduler pushes, and edit_text paths so all
operator-facing replies share the same formatting stack (Option B).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

from src.telegram.operator_messages import readable_plain

from src.telegram.rich_api import (
    bot_can_attempt_rich,
    extract_message_id,
    send_rich_message,
)
from src.telegram.stream_format import (
    char_len,
    markdown_to_telegram_html,
    normalize_markdown_tables_for_mobile,
    sanitize_llm_markdown,
    truncate_message,
    utf16_len,
)

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class DeliveredChunk:
    """One successfully delivered message chunk."""

    message_id: int | None
    source_text: str
    render: str  # rich | html | plain
    raw: Any = None


def resolve_format_mode(explicit: str | None = None) -> str:
    if explicit in {"rich", "html", "plain"}:
        return explicit
    try:
        from config.settings import CONFIG

        mode = getattr(getattr(CONFIG.telegram, "streaming", None), "format_mode", "html")
        if mode in {"rich", "html", "plain"}:
            return mode
    except Exception:
        pass
    return "html"


def resolve_limits() -> tuple[int, int]:
    """Return (rich_max, regular_max)."""
    rich_max, regular_max = 32768, 4096
    try:
        from config.settings import CONFIG

        streaming = getattr(CONFIG.telegram, "streaming", None)
        if streaming is not None:
            rich_max = int(getattr(streaming, "max_rich_message_length", rich_max))
            regular_max = int(getattr(streaming, "max_message_length", regular_max))
    except Exception:
        pass
    return rich_max, regular_max


async def send_formatted_text(
    bot: Any,
    *,
    chat_id: int,
    text: str,
    reply_to_message_id: int | None = None,
    reply_markup: Any = None,
    message_thread_id: int | None = None,
    format_mode: str | None = None,
    disable_web_page_preview: bool = True,
) -> list[DeliveredChunk]:
    """Send *text* with rich→HTML→plain cascade. Chunks long content."""
    source = sanitize_llm_markdown(text) if text else ""
    if not source:
        source = "(No content)"

    mode = resolve_format_mode(format_mode)
    rich_max, regular_max = resolve_limits()
    use_rich = mode == "rich" and bot_can_attempt_rich(bot)
    if not use_rich:
        source = normalize_markdown_tables_for_mobile(source)
    max_len = rich_max if use_rich else regular_max
    len_fn = char_len if use_rich else utf16_len
    chunks = truncate_message(source, max_len, len_fn=len_fn)

    delivered: list[DeliveredChunk] = []
    for index, chunk in enumerate(chunks):
        markup = reply_markup if index == len(chunks) - 1 else None
        # Only first chunk gets reply_to to avoid reply spam.
        reply_to = reply_to_message_id if index == 0 else None
        item = await _send_one_chunk(
            bot,
            chat_id=chat_id,
            source_text=chunk,
            reply_to_message_id=reply_to,
            reply_markup=markup,
            message_thread_id=message_thread_id,
            prefer_mode=mode,
            disable_web_page_preview=disable_web_page_preview,
        )
        delivered.append(item)
        # If rich failed mid-batch, subsequent chunks use degraded mode.
        if item.render != "rich" and mode == "rich":
            mode = item.render
    return delivered


async def _send_one_chunk(
    bot: Any,
    *,
    chat_id: int,
    source_text: str,
    reply_to_message_id: int | None,
    reply_markup: Any,
    message_thread_id: int | None,
    prefer_mode: str,
    disable_web_page_preview: bool,
) -> DeliveredChunk:
    mode = prefer_mode

    if mode == "rich" and bot_can_attempt_rich(bot):
        try:
            sent = await send_rich_message(
                bot,
                chat_id=chat_id,
                markdown=source_text,
                reply_to_message_id=reply_to_message_id,
                message_thread_id=message_thread_id,
            )
            mid = extract_message_id(sent)
            if mid is not None:
                if reply_markup is not None:
                    await _try_set_reply_markup(bot, chat_id, mid, reply_markup)
                logger.info("Delivered rich message chat=%s mid=%s", chat_id, mid)
                return DeliveredChunk(message_id=mid, source_text=source_text, render="rich", raw=sent)
            logger.warning("Rich send produced no message_id; falling back to HTML")
        except Exception:
            logger.warning("Rich send failed; falling back to HTML", exc_info=True)
        mode = "html"

    if mode in {"rich", "html"}:
        try:
            from telegram.constants import ParseMode

            html_body = markdown_to_telegram_html(source_text)
            kwargs: dict[str, Any] = {
                "chat_id": chat_id,
                "text": html_body,
                "parse_mode": ParseMode.HTML,
                "reply_to_message_id": reply_to_message_id,
                "reply_markup": reply_markup,
            }
            if message_thread_id is not None:
                kwargs["message_thread_id"] = message_thread_id
            if disable_web_page_preview:
                kwargs["disable_web_page_preview"] = True
            sent = await _call(bot, "send_message", **kwargs)
            mid = extract_message_id(sent)
            if mid is not None or sent is not None:
                return DeliveredChunk(
                    message_id=mid,
                    source_text=source_text,
                    render="html",
                    raw=sent,
                )
        except Exception:
            logger.debug("HTML send failed; falling back to plain", exc_info=True)

    kwargs = {
        "chat_id": chat_id,
        "text": readable_plain(source_text),
        "reply_to_message_id": reply_to_message_id,
        "reply_markup": reply_markup,
    }
    if message_thread_id is not None:
        kwargs["message_thread_id"] = message_thread_id
    sent = await _call(bot, "send_message", **kwargs)
    return DeliveredChunk(
        message_id=extract_message_id(sent),
        source_text=source_text,
        render="plain",
        raw=sent,
    )


async def edit_formatted_text(
    bot: Any,
    *,
    chat_id: int,
    message_id: int,
    text: str,
    reply_markup: Any = None,
    format_mode: str | None = None,
) -> str:
    """Edit a message with rich→HTML→plain cascade. Returns render mode used."""
    source = sanitize_llm_markdown(text) if text else ""
    if not source:
        source = "(No content)"
    mode = resolve_format_mode(format_mode)
    use_rich = mode == "rich" and bot_can_attempt_rich(bot)
    if not use_rich:
        source = normalize_markdown_tables_for_mobile(source)

    if use_rich:
        try:
            do_api = _method(bot, "do_api_request")
            edit_rich = _method(bot, "edit_message_text")
            api_kwargs: dict[str, Any] = {
                "chat_id": chat_id,
                "message_id": message_id,
                "rich_message": {"markdown": source},
            }
            if reply_markup is not None:
                api_kwargs["reply_markup"] = reply_markup
            if do_api is not None:
                await _await_maybe(do_api("editMessageText", api_kwargs=api_kwargs))
                return "rich"
            if edit_rich is not None:
                # PTB may not support rich_message kw yet — try and fall through.
                await _await_maybe(edit_rich(**api_kwargs))
                return "rich"
        except Exception:
            logger.debug("Rich edit failed; falling back to HTML", exc_info=True)
        mode = "html"

    if mode in {"rich", "html"}:
        try:
            from telegram.constants import ParseMode

            html_body = markdown_to_telegram_html(source)
            kwargs: dict[str, Any] = {
                "chat_id": chat_id,
                "message_id": message_id,
                "text": html_body,
                "parse_mode": ParseMode.HTML,
            }
            if reply_markup is not None:
                kwargs["reply_markup"] = reply_markup
            await _call(bot, "edit_message_text", **kwargs)
            return "html"
        except Exception:
            logger.debug("HTML edit failed; falling back to plain", exc_info=True)

    kwargs = {
        "chat_id": chat_id,
        "message_id": message_id,
        "text": readable_plain(source),
    }
    if reply_markup is not None:
        kwargs["reply_markup"] = reply_markup
    await _call(bot, "edit_message_text", **kwargs)
    return "plain"


async def _try_set_reply_markup(bot: Any, chat_id: int, message_id: int, reply_markup: Any) -> None:
    try:
        await _call(
            bot,
            "edit_message_reply_markup",
            chat_id=chat_id,
            message_id=message_id,
            reply_markup=reply_markup,
        )
    except Exception:
        logger.debug("Could not attach reply_markup after rich send", exc_info=True)


def _method(bot: Any, name: str) -> Any | None:
    inst = getattr(bot, "__dict__", None)
    if isinstance(inst, dict) and name in inst and callable(inst[name]):
        return inst[name]
    cls = type(bot)
    if getattr(cls, "__module__", "").startswith("unittest.mock"):
        # Explicit instance attrs only for mocks
        return None
    attr = getattr(cls, name, None)
    if callable(attr):
        return getattr(bot, name)
    # Fallback getattr for real bots / simple mocks used in tests
    method = getattr(bot, name, None)
    return method if callable(method) else None


async def _call(bot: Any, method_name: str, **kwargs: Any) -> Any:
    method = getattr(bot, method_name, None)
    if method is None:
        raise AttributeError(f"bot has no method {method_name}")
    return await _await_maybe(method(**kwargs))


async def _await_maybe(result: Any) -> Any:
    import inspect

    if inspect.isawaitable(result):
        return await result
    return result
