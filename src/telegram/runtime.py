"""
Shared Telegram runtime helpers for sending persisted messages.
"""

from __future__ import annotations

import inspect
import logging
import sqlite3
import time
from dataclasses import dataclass

from src.events.bus import Event, event_bus
from src.telegram.i18n import t
from src.telegram.persistence import record_outbound_message

logger = logging.getLogger(__name__)


@dataclass
class ProgressStep:
    """One streamed progress step."""
    name: str
    status: str = "running"  # pending | running | done | error
    detail: str | None = None


def chunk_text(text: str, max_len: int = 4000) -> list[str]:
    """Split text into chunks that fit Telegram's message limit."""
    if len(text) <= max_len:
        return [text]
    chunks = []
    remaining = text
    while remaining:
        if len(remaining) <= max_len:
            chunks.append(remaining)
            break
        split = remaining.rfind("\n", 0, max_len)
        if split < max_len // 2:
            split = max_len
        chunks.append(remaining[:split])
        remaining = remaining[split:].lstrip("\n")
    return chunks


async def _call_optional_async(method, /, *args, **kwargs):
    """Call a bot method when present, awaiting only if needed."""
    if method is None:
        return None
    result = method(*args, **kwargs)
    if inspect.isawaitable(result):
        return await result
    return result


class TelegramProgressStreamer:
    """Best-effort Telegram progress streamer with draft-first fallback logic."""

    def __init__(
        self,
        bot,
        conn: sqlite3.Connection | None,
        *,
        chat_id: int,
        reply_to_message_id: int | None = None,
        project_id: str | None = None,
        session_id: str | None = None,
        operator_id: str = "default_operator",
    ) -> None:
        self.bot = bot
        self.conn = conn
        self.chat_id = chat_id
        self.reply_to_message_id = reply_to_message_id
        self.project_id = project_id
        self.session_id = session_id
        self.operator_id = operator_id
        self.mode: str | None = None
        self.draft_id = int(time.time() * 1000) % 2_000_000_000 or 1
        self.progress_message_id: int | None = None
        self._started = False
        self._steps: list[ProgressStep] = []

    async def start(self, text: str) -> None:
        if not self._started:
            await self._send_typing()
            self._started = True
        await self.update(text)

    async def update(self, text: str) -> None:
        content = chunk_text(text, max_len=4096)[-1]
        self._publish_step_event(content=content)

        if self.mode in (None, "draft"):
            sent = await self._try_send_draft(content)
            if sent:
                self.mode = "draft"
                return

        if self.mode is None:
            await self._start_fallback_message(content)
            return

        if self.mode == "edit":
            await self._edit_fallback_message(content)

    async def start_step(self, name: str, detail: str | None = None) -> None:
        step = self._find_step(name)
        if step is None:
            step = ProgressStep(name=name, status="running", detail=detail)
            self._steps.append(step)
        else:
            step.status = "running"
            step.detail = detail
        await self.start(self._render_steps())
        self._publish_step_event(step_name=name, status="running", content=self._render_steps())

    async def complete_step(self, name: str, detail: str | None = None) -> None:
        step = self._ensure_step(name)
        step.status = "done"
        if detail is not None:
            step.detail = detail
        await self.update(self._render_steps())
        self._publish_step_event(step_name=name, status="done", content=self._render_steps())

    async def fail_step(self, name: str, detail: str | None = None) -> None:
        step = self._ensure_step(name)
        step.status = "error"
        if detail is not None:
            step.detail = detail
        await self.update(self._render_steps())
        self._publish_step_event(step_name=name, status="error", content=self._render_steps())

    def _find_step(self, name: str) -> ProgressStep | None:
        for step in self._steps:
            if step.name == name:
                return step
        return None

    def _ensure_step(self, name: str) -> ProgressStep:
        step = self._find_step(name)
        if step is None:
            step = ProgressStep(name=name)
            self._steps.append(step)
        return step

    # Resolve translations only at render time; import-only tooling must not open SQLite.
    _STEP_EMOJI: tuple[tuple[str, str], ...] = (
        ("progress.receive_instruction", "📨"),
        ("progress.receive_session_input", "📨"),
        ("progress.classify_intent", "🔍"),
        ("progress.run_reasoning", "🧠"),
        ("progress.run_write_layer", "🧠"),
        ("progress.build_context", "✏️"),
        ("progress.load_context", "📂"),
        ("progress.agent_round", "🤖"),
        ("progress.execute_tool", "⚙️"),
        ("progress.send_final_reply", "📤"),
        ("progress.text_recovery", "🔧"),
    )

    def _step_emoji(self, name: str) -> str:
        for prefix, emoji in self._STEP_EMOJI:
            if name.startswith(t(prefix)):
                return emoji
        return "▸"

    def _render_steps(self) -> str:
        status_icon = {
            "pending": "⬜",
            "running": "⏳",
            "done": "✅",
            "error": "❌",
        }
        lines = []
        for step in self._steps:
            icon = status_icon.get(step.status, "⬜")
            emoji = self._step_emoji(step.name)
            line = f"{icon} {emoji} {step.name}"
            if step.detail:
                line += f": {step.detail}"
            lines.append(line)
        return "\n".join(lines) if lines else t("progress.working")

    def _publish_step_event(
        self,
        *,
        content: str,
        step_name: str | None = None,
        status: str | None = None,
    ) -> None:
        event_bus.publish(Event(
            event_type="step_update",
            project_id=self.project_id,
            surface="telegram",
            detail={
                "content": content,
                "step_name": step_name,
                "status": status,
                "streaming_mode": self.mode or "pending",
                "session_id": self.session_id,
            },
        ))

    async def _send_typing(self) -> None:
        try:
            await _call_optional_async(
                getattr(self.bot, "send_chat_action", None),
                chat_id=self.chat_id,
                action="typing",
            )
        except Exception:
            logger.debug("Telegram typing indicator failed", exc_info=True)

    async def _try_send_draft(self, text: str) -> bool:
        try:
            result = await _call_optional_async(
                getattr(self.bot, "send_message_draft", None),
                chat_id=self.chat_id,
                draft_id=self.draft_id,
                text=text,
            )
            return bool(result)
        except Exception:
            logger.debug("Telegram send_message_draft failed; falling back to edit flow", exc_info=True)
            return False

    async def _start_fallback_message(self, text: str) -> None:
        sent = await self.bot.send_message(
            chat_id=self.chat_id,
            text=text,
            reply_to_message_id=self.reply_to_message_id,
        )
        self.progress_message_id = getattr(sent, "message_id", None)
        self.mode = "edit"
        self._persist_progress_message(
            telegram_message_id=self.progress_message_id,
            text=text,
            metadata={"streaming": True, "streaming_mode": "edit"},
        )

    async def _edit_fallback_message(self, text: str) -> None:
        if self.progress_message_id is None:
            await self._start_fallback_message(text)
            return
        try:
            await _call_optional_async(
                getattr(self.bot, "edit_message_text", None),
                chat_id=self.chat_id,
                message_id=self.progress_message_id,
                text=text,
            )
        except Exception:
            logger.debug("Telegram edit_message_text failed during streaming", exc_info=True)
        self._persist_progress_message(
            telegram_message_id=self.progress_message_id,
            text=text,
            metadata={"streaming": True, "streaming_mode": "edit"},
        )

    def _persist_progress_message(
        self,
        *,
        telegram_message_id: int | None,
        text: str,
        metadata: dict,
    ) -> None:
        kwargs = dict(
            chat_id=self.chat_id,
            telegram_message_id=telegram_message_id,
            text=text,
            message_kind="stream_progress",
            reply_to_telegram_message_id=self.reply_to_message_id,
            project_id=self.project_id,
            session_id=self.session_id,
            operator_id=self.operator_id,
            metadata=metadata,
        )
        try:
            if self.conn is not None:
                record_outbound_message(self.conn, **kwargs)
                return
            from src.db.connection import scoped_write

            with scoped_write() as wconn:
                record_outbound_message(wconn, **kwargs)
        except Exception:
            logger.exception("Failed to persist progress stream message")

    async def dismiss(self) -> None:
        """Delete the progress message after the final reply is sent. Best-effort only."""
        if not self.progress_message_id:
            return
        msg_id = self.progress_message_id
        self.progress_message_id = None
        try:
            await self.bot.delete_message(
                chat_id=self.chat_id,
                message_id=msg_id,
            )
        except Exception:
            logger.debug("Progress message dismiss failed (non-critical)", exc_info=True)


async def send_persisted_message(
    bot,
    conn: sqlite3.Connection | None = None,
    *,
    chat_id: int,
    text: str,
    reply_markup=None,
    reply_to_message_id: int | None = None,
    project_id: str | None = None,
    session_id: str | None = None,
    message_kind: str = "text",
    metadata: dict | None = None,
    object_links: list[dict] | None = None,
    operator_id: str = "default_operator",
    format_mode: str | None = None,
    require_persistence: bool = False,
) -> list[int]:
    """Send Telegram text (rich→HTML→plain), persist every chunk, return IDs.

    If *conn* is ``None`` (the default), each chunk's DB persist uses its
    own short-lived ``scoped_write()`` — no write lock is held during the
    Telegram network call.  Callers no longer need to wrap this function
    in ``scoped_raw_write()``. With ``require_persistence=True``, a failed
    local record after a remote send raises for caller reconciliation; it
    never makes that send safe to retry automatically.
    """
    if not text or not text.strip():
        logger.warning("send_persisted_message called with empty text — substituting fallback")
        text = "(No content)"

    from src.telegram.message_delivery import send_formatted_text

    delivered = await send_formatted_text(
        bot,
        chat_id=chat_id,
        text=text,
        reply_to_message_id=reply_to_message_id,
        reply_markup=reply_markup,
        format_mode=format_mode,
    )

    if require_persistence and not delivered:
        raise RuntimeError("Telegram send returned no delivered chunks; outcome requires reconciliation")

    message_ids: list[int] = []
    for index, item in enumerate(delivered):
        if require_persistence and (type(item.message_id) is not int or item.message_id <= 0):
            raise RuntimeError("Telegram send returned no usable message ID; outcome requires reconciliation")
        message_id = item.message_id
        chunk = item.source_text
        meta = dict(metadata or {})
        meta.setdefault("render", item.render)
        try:
            if conn is not None:
                persisted_id = record_outbound_message(
                    conn,
                    chat_id=chat_id,
                    telegram_message_id=message_id,
                    text=chunk,
                    message_kind=message_kind,
                    reply_to_telegram_message_id=reply_to_message_id,
                    project_id=project_id,
                    session_id=session_id,
                    operator_id=operator_id,
                    metadata=meta,
                    object_links=object_links if index == len(delivered) - 1 else None,
                    strict_audit=require_persistence,
                    commit=not require_persistence,
                )
            else:
                from src.db.connection import scoped_write

                with scoped_write() as wconn:
                    persisted_id = record_outbound_message(
                        wconn,
                        chat_id=chat_id,
                        telegram_message_id=message_id,
                        text=chunk,
                        message_kind=message_kind,
                        reply_to_telegram_message_id=reply_to_message_id,
                        project_id=project_id,
                        session_id=session_id,
                        operator_id=operator_id,
                        metadata=meta,
                        object_links=object_links if index == len(delivered) - 1 else None,
                        strict_audit=require_persistence,
                        commit=not require_persistence,
                    )
        except Exception:
            logger.exception("Failed to persist outbound message (Telegram send succeeded)")
            if require_persistence:
                raise
            if conn is not None:
                try:
                    conn.rollback()
                except Exception:
                    pass
            persisted_id = message_id
        message_ids.append(persisted_id if persisted_id is not None else 0)
    return message_ids
