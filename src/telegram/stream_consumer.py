"""
Streaming text consumer — bridges sync LLM thread to async Telegram delivery.

Transport stack (private chats), controlled by format_mode:
  - rich (opt-in): sendRichMessageDraft → sendRichMessage
  - html (default): sendMessageDraft → sendMessage(HTML) — all clients
  - plain: sendMessage without parse_mode
  Falls back down the cascade on API failure.
"""

from __future__ import annotations

import asyncio
import inspect
import logging
import queue
import time
from typing import Any

from src.telegram.rich_api import (
    bot_can_attempt_rich,
    extract_message_id,
    send_rich_message,
    send_rich_message_draft,
)
from src.telegram.stream_config import StreamConsumerConfig
from src.telegram.stream_format import (
    char_len,
    clean_for_display,
    markdown_to_telegram_html,
    sanitize_llm_markdown,
    truncate_message,
    utf16_len,
)

logger = logging.getLogger(__name__)

_DONE = object()


class StreamConsumer:
    """Accumulates LLM deltas and streams them to Telegram."""

    def __init__(
        self,
        bot: Any,
        chat_id: int,
        *,
        config: StreamConsumerConfig | None = None,
        reply_to_message_id: int | None = None,
        message_thread_id: int | None = None,
        chat_type: str | None = None,
    ) -> None:
        self.bot = bot
        self.chat_id = chat_id
        self.config = config or StreamConsumerConfig()
        self.reply_to_message_id = reply_to_message_id
        self.message_thread_id = message_thread_id
        self.chat_type = (chat_type or "").lower()

        self._queue: queue.Queue[str | None | object] = queue.Queue()
        self.message_id: int | None = None
        self._accumulated = ""
        self._last_edit_time = 0.0
        self._last_sent_text = ""
        self._current_edit_interval = self.config.edit_interval
        self._flood_strikes = 0
        self._edit_supported = True
        self._fallback_pending = ""
        self._active = False
        self._finished = False
        self._first_send_time: float | None = None
        self._transport: str | None = None
        self._draft_id = int(time.time() * 1000) % 2_000_000_000 or 1
        self._draft_active = False
        self._rich_ok = (
            self.config.format_mode == "rich" and bot_can_attempt_rich(bot)
        )
        self._format_mode = self.config.format_mode  # may degrade: rich→html→plain

    # ── Sync producer (LLM thread) ────────────────────────────────────

    def on_delta(self, text: str | None) -> None:
        self._queue.put(text)

    def finish(self) -> None:
        self._queue.put(_DONE)

    @property
    def is_active(self) -> bool:
        return self._active

    @property
    def should_finalize_stream(self) -> bool:
        """True when a draft or editable stream was started and needs finalize."""
        return self.message_id is not None or self._draft_active

    def supports_draft_streaming(self) -> bool:
        if self.config.transport == "edit":
            return False
        if self.chat_type not in {"private", "dm"}:
            return False
        if self._rich_ok:
            return True
        return hasattr(self.bot, "send_message_draft")

    def _resolve_transport(self) -> str:
        if self._transport is not None:
            return self._transport
        if self.config.transport == "draft":
            self._transport = "draft" if self.supports_draft_streaming() else "edit"
        elif self.config.transport == "edit":
            self._transport = "edit"
        else:
            self._transport = "draft" if self.supports_draft_streaming() else "edit"
        return self._transport

    def _chunk_limit(self) -> tuple[int, Any]:
        if self._rich_ok and self._format_mode == "rich":
            return self.config.max_rich_message_length, char_len
        return self.config.max_message_length, utf16_len

    # ── Async consumer (event loop) ─────────────────────────────────

    async def run(self) -> None:
        self._active = True
        last_typing_time = 0.0
        try:
            while True:
                now = time.monotonic()
                if now - last_typing_time >= self.config.typing_refresh_sec:
                    await self._send_typing()
                    last_typing_time = now

                items: list[str | None | object] = []
                try:
                    while True:
                        items.append(self._queue.get_nowait())
                except queue.Empty:
                    pass

                got_done = False
                for item in items:
                    if item is _DONE:
                        got_done = True
                    elif item is None:
                        self._accumulated = ""
                    elif isinstance(item, str):
                        self._accumulated += item

                display = sanitize_llm_markdown(self._accumulated)
                now = time.monotonic()
                elapsed = now - self._last_edit_time
                char_delta = len(display) - len(self._last_sent_text)
                should_flush = (
                    got_done
                    or (elapsed >= self._current_edit_interval and char_delta > 0)
                    or char_delta >= self.config.buffer_threshold
                )

                if should_flush and display:
                    await self._send_or_edit(display, finalize=False)

                if got_done:
                    break

                await asyncio.sleep(0.05)
        finally:
            self._active = False
            self._finished = True

    async def finalize(self, final_text: str) -> bool:
        """Apply the final reply; return False when delivery may be incomplete.

        Progressive stream bubbles are plain/draft-only. The final message is
        always sent via the unified rich→HTML→plain stack (new message), so
        tables/headings can use sendRichMessage instead of edit_message_text.
        """
        final_text = sanitize_llm_markdown(final_text)
        if not final_text:
            return False

        if self._fallback_pending:
            # Extra overflow text is included in final_text from the agent; drop buffer.
            self._fallback_pending = ""

        from src.telegram.message_delivery import send_formatted_text

        old_id = self.message_id
        try:
            delivered = await send_formatted_text(
                self.bot,
                chat_id=self.chat_id,
                text=final_text,
                reply_to_message_id=self.reply_to_message_id,
                message_thread_id=self.message_thread_id,
                format_mode=self.config.format_mode,
            )
        except Exception:
            logger.exception("Stream finalize via message_delivery failed")
            delivered = []

        if not delivered:
            # Last-resort legacy path
            if self._draft_active:
                await self._send_final_message(final_text)
            else:
                await self._send_or_edit(final_text, finalize=True)
            return self._delivery_matches(final_text)

        self._last_sent_text = final_text
        self._last_edit_time = time.monotonic()
        self._draft_active = False
        if delivered[0].message_id is not None:
            self.message_id = delivered[0].message_id
            if self._first_send_time is None:
                self._first_send_time = time.monotonic()
        # Remove plain progressive bubble so the user only sees the rich/HTML final.
        if (
            old_id is not None
            and delivered[0].message_id is not None
            and old_id != delivered[0].message_id
        ):
            try:
                await self._call_bot(
                    "delete_message",
                    chat_id=self.chat_id,
                    message_id=old_id,
                )
            except Exception:
                logger.debug("Could not delete stream bubble %s", old_id, exc_info=True)
        return True

    def _delivery_matches(self, final_text: str) -> bool:
        if not self._last_sent_text:
            return False
        if self._last_sent_text == final_text:
            return True
        if final_text.startswith(self._last_sent_text):
            return len(self._last_sent_text) >= max(20, int(len(final_text) * 0.85))
        return False

    async def _send_or_edit(self, text: str, *, finalize: bool) -> None:
        if not text:
            return

        transport = self._resolve_transport()
        max_len, len_fn = self._chunk_limit()
        # Rich drafts animate without a cursor glyph (avoids breaking GFM tables).
        if finalize or (transport == "draft" and self._rich_ok):
            display = text
        else:
            display = f"{text}{self.config.cursor}"
        chunks = truncate_message(display, max_len, len_fn=len_fn)
        primary = chunks[0]

        if transport == "draft" and not finalize:
            sent = await self._send_draft(primary)
            if sent:
                self._draft_active = True
                self._last_sent_text = text
                self._last_edit_time = time.monotonic()
                if self._first_send_time is None:
                    self._first_send_time = self._last_edit_time
                if len(chunks) > 1:
                    self._fallback_pending = "".join(chunks[1:])
                return
            self._transport = "edit"
            transport = "edit"

        if self._should_send_fresh_message() and self.message_id is not None:
            self.message_id = None
            self._last_sent_text = ""

        if self.message_id is None:
            ok = await self._send_message(primary, finalize=finalize)
            if ok and len(chunks) > 1:
                for extra in chunks[1:]:
                    await self._send_message(extra, finalize=True)
        elif self._edit_supported:
            ok = await self._edit_message(primary, finalize=finalize)
            if ok and len(chunks) > 1:
                for extra in chunks[1:]:
                    await self._send_message(extra, finalize=True)
        else:
            unseen = display[len(self._last_sent_text) :]
            if unseen:
                self._fallback_pending += unseen

        if not finalize:
            self._last_sent_text = text

    def _should_send_fresh_message(self) -> bool:
        threshold = self.config.fresh_final_after_seconds
        if threshold <= 0 or self._first_send_time is None:
            return False
        return time.monotonic() - self._first_send_time >= threshold

    async def _send_typing(self) -> None:
        try:
            kwargs: dict[str, Any] = {"chat_id": self.chat_id, "action": "typing"}
            if self.message_thread_id is not None:
                kwargs["message_thread_id"] = self.message_thread_id
            await self._call_bot("send_chat_action", **kwargs)
        except Exception:
            logger.debug("Stream typing indicator failed", exc_info=True)

    async def _send_draft(self, text: str) -> bool:
        # 1) Rich draft
        if self._rich_ok:
            try:
                ok = await send_rich_message_draft(
                    self.bot,
                    chat_id=self.chat_id,
                    draft_id=self._draft_id,
                    markdown=text or None,
                    html=None if text else "<tg-thinking>Thinking...</tg-thinking>",
                    message_thread_id=self.message_thread_id,
                )
                if ok:
                    return True
            except Exception:
                logger.debug("sendRichMessageDraft failed; degrading", exc_info=True)
            self._rich_ok = False
            if self._format_mode == "rich":
                self._format_mode = "html"

        # 2) Plain text draft
        try:
            kwargs: dict[str, Any] = {
                "chat_id": self.chat_id,
                "draft_id": self._draft_id,
                "text": text,
            }
            if self.message_thread_id is not None:
                kwargs["message_thread_id"] = self.message_thread_id
            result = await self._call_bot("send_message_draft", **kwargs)
            return bool(result)
        except Exception:
            logger.debug("send_message_draft failed; falling back to edit", exc_info=True)
            return False

    async def _send_message(self, text: str, *, finalize: bool) -> bool:
        if finalize and self._rich_ok and self._format_mode == "rich":
            if await self._send_rich_final(text):
                return True
            self._rich_ok = False
            self._format_mode = "html"

        kwargs: dict[str, Any] = {
            "chat_id": self.chat_id,
            "text": text,
            "reply_to_message_id": self.reply_to_message_id,
        }
        if self.message_thread_id is not None:
            kwargs["message_thread_id"] = self.message_thread_id
        if finalize:
            self._apply_finalize_format(kwargs, text)

        try:
            sent = await self._call_bot("send_message", **kwargs)
            message_id = getattr(sent, "message_id", None)
            if self.message_id is None and message_id is not None:
                self.message_id = message_id
                self._first_send_time = time.monotonic()
            self._last_sent_text = text
            self._last_edit_time = time.monotonic()
            return True
        except Exception:
            if finalize and kwargs.get("parse_mode") is not None:
                try:
                    kwargs.pop("parse_mode", None)
                    kwargs["text"] = text
                    sent = await self._call_bot("send_message", **kwargs)
                    message_id = getattr(sent, "message_id", None)
                    if self.message_id is None and message_id is not None:
                        self.message_id = message_id
                    self._last_sent_text = text
                    self._last_edit_time = time.monotonic()
                    self._format_mode = "plain"
                    return True
                except Exception:
                    logger.debug("Stream plain send failed", exc_info=True)
                    return False
            logger.debug("Stream initial send failed", exc_info=True)
            return False

    async def _send_rich_final(self, text: str) -> bool:
        try:
            sent = await send_rich_message(
                self.bot,
                chat_id=self.chat_id,
                markdown=text,
                reply_to_message_id=self.reply_to_message_id,
                message_thread_id=self.message_thread_id,
            )
            if sent is None or sent is False:
                return False
            message_id = extract_message_id(sent)
            if message_id is None:
                return False
            if self.message_id is None:
                self.message_id = message_id
                self._first_send_time = time.monotonic()
            self._last_sent_text = text
            self._last_edit_time = time.monotonic()
            return True
        except Exception:
            logger.debug("sendRichMessage failed", exc_info=True)
            return False

    async def _edit_message(self, text: str, *, finalize: bool) -> bool:
        if self.message_id is None:
            return await self._send_message(text, finalize=finalize)
        if text == self._last_sent_text and not finalize:
            return True
        try:
            kwargs: dict[str, Any] = {
                "chat_id": self.chat_id,
                "message_id": self.message_id,
                "text": text,
            }
            if finalize:
                self._apply_finalize_format(kwargs, text)
            try:
                await self._call_bot("edit_message_text", **kwargs)
            except Exception as markdown_exc:
                if finalize and kwargs.get("parse_mode") is not None:
                    # Prefer plain *converted* body without entities over raw markdown.
                    kwargs.pop("parse_mode", None)
                    try:
                        from src.telegram.stream_format import markdown_to_telegram_html
                        import re

                        # Strip tags for plain-safe fallback (keep content, not **)
                        plain_html = markdown_to_telegram_html(text)
                        kwargs["text"] = re.sub(r"<[^>]+>", "", plain_html)
                    except Exception:
                        kwargs["text"] = text
                    await self._call_bot("edit_message_text", **kwargs)
                    self._format_mode = "plain"
                else:
                    raise markdown_exc
            self._last_sent_text = text
            self._last_edit_time = time.monotonic()
            return True
        except Exception as exc:
            return self._handle_edit_failure(exc)

    async def _send_final_message(self, text: str) -> None:
        max_len, len_fn = self._chunk_limit()
        # Prefer rich limit when still enabled; else regular 4096.
        if not self._rich_ok:
            max_len, len_fn = self.config.max_message_length, utf16_len
        chunks = truncate_message(text, max_len, len_fn=len_fn)
        for chunk in chunks:
            await self._send_message(chunk, finalize=True)
        self._draft_active = False

    async def _send_fallback_chunks(self, text: str) -> None:
        for chunk in truncate_message(text, self.config.max_message_length, len_fn=utf16_len):
            await self._send_message(chunk, finalize=True)

    def _handle_edit_failure(self, exc: Exception) -> bool:
        err = str(exc).lower()
        if "message is not modified" in err:
            return True
        if any(token in err for token in ("flood", "retry", "too many")):
            self._flood_strikes += 1
            self._current_edit_interval = min(
                self._current_edit_interval * 2,
                self.config.max_edit_interval,
            )
            self._last_edit_time = time.monotonic() + self._current_edit_interval
            if self._flood_strikes >= self.config.max_flood_strikes:
                self._edit_supported = False
                logger.warning(
                    "Stream edit disabled after %d flood strikes (chat=%s)",
                    self._flood_strikes,
                    self.chat_id,
                )
            return False
        logger.debug("Stream edit failed", exc_info=True)
        return False

    def _apply_finalize_format(self, kwargs: dict[str, Any], text: str) -> None:
        """Attach parse_mode for regular messages (HTML preferred over MarkdownV2)."""
        mode = self._format_mode
        if mode == "rich":
            # Rich final uses sendRichMessage; if we land here, use HTML.
            mode = "html"
        if mode == "plain":
            kwargs["text"] = text
            return
        try:
            from telegram.constants import ParseMode

            html_body = markdown_to_telegram_html(text)
            kwargs["text"] = html_body
            kwargs["parse_mode"] = ParseMode.HTML
            kwargs.setdefault("disable_web_page_preview", True)
            self._format_mode = "html"
        except Exception:
            kwargs["text"] = text

    async def _call_bot(self, method_name: str, /, **kwargs: Any) -> Any:
        method = getattr(self.bot, method_name, None)
        if method is None:
            return None
        result = method(**kwargs)
        if inspect.isawaitable(result):
            return await result
        return result
