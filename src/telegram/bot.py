"""
Telegram Bot for AgentFIXBPRO.
Commands: /start, /scan, /status, /pending, /approve, /reject, /brief, /note,
          /track, /close, /reopen, /query, /newproject, /report, /batch,
          /ticket, /support, /resolve, /help
"""

from __future__ import annotations

import asyncio
import sqlite3
import threading
from contextlib import contextmanager
from functools import wraps
from typing import TYPE_CHECKING, Callable, Iterator

if TYPE_CHECKING:
    from src.telegram.stream_consumer import StreamConsumer

from telegram import BotCommand, Update
from telegram.ext import Application, CallbackQueryHandler, CommandHandler, MessageHandler, filters
from telegram.request import HTTPXRequest

from src.auth.rbac import P_BUSINESS_WRITE, P_PROJECT_CLOSE, permissions_for_role
from src.observability.progress import ProgressCallback
from src.telegram.cards import InteractionCard, format_card_text
from src.telegram.handlers_agent_cmds import AgentCommandsMixin
from src.telegram.handlers_commands import CommandsMixin
from src.telegram.handlers_freetext import FreetextMixin
from src.telegram.handlers_sessions import SessionsMixin
from src.telegram.handlers_support_cmds import SupportCommandsMixin
from src.telegram.markup import card_to_markup
from src.telegram.operator_binding import resolve_telegram_operator, telegram_user_id_from_update
from src.telegram.persistence import record_inbound_message
from src.telegram.runtime import TelegramProgressStreamer, chunk_text, send_persisted_message


def build_telegram_command_menu() -> list[BotCommand]:
    """Slash commands shown in Telegram's command menu (set_my_commands)."""
    return [
        BotCommand("start", "Home screen"),
        BotCommand("scan", "Portfolio scan"),
        BotCommand("status", "Project status"),
        BotCommand("session", "Verified FIX session configuration"),
        BotCommand("pending", "Pending drafts / reviews"),
        BotCommand("brief", "Morning brief"),
        BotCommand("note", "Store meeting/call note"),
        BotCommand("track", "Create follow-up"),
        BotCommand("close", "Close a project"),
        BotCommand("reopen", "Reopen a project"),
        BotCommand("query", "Query projects"),
        BotCommand("newproject", "New project intake"),
        BotCommand("report", "Generate report"),
        BotCommand("batch", "Batch update"),
        BotCommand("ticket", "Create support ticket"),
        BotCommand("support", "Open support tickets"),
        BotCommand("resolve", "Record resolution"),
        BotCommand("notification", "Notification mode (silent/active)"),
        BotCommand("thinking", "Thinking debug window (on/off)"),
        BotCommand("lang", "Language EN/ZH"),
        BotCommand("help", "Show help"),
    ]


# Only audited read commands can run without a linked, active Telegram actor.
# /query may create a clarification session; /pending emits actionable cards.
_READ_ONLY_COMMANDS = frozenset({
    "start", "scan", "portfolio", "status", "session", "brief",
    "report", "activity", "support", "help",
})
_BUSINESS_WRITE_COMMANDS = frozenset({
    "approve", "reject", "note", "track", "flag", "set_preference",
    "delete_preference", "newproject", "batch", "signal_positive", "skip",
    "reply", "ticket", "resolve",
})
_PROJECT_CLOSE_COMMANDS = frozenset({"close", "reopen"})


class TelegramBot(CommandsMixin, AgentCommandsMixin, SupportCommandsMixin, SessionsMixin, FreetextMixin):
    """AgentFIXBPRO Telegram bot."""

    def __init__(
        self,
        token: str,
        chat_id: int,
        conn: sqlite3.Connection | None = None,
        *,
        allowed_chat_ids: list[int] | None = None,
        allow_all_chats: bool = False,
    ):
        self.token = token
        self.chat_id = chat_id
        self.allow_all_chats = allow_all_chats
        allowed = allowed_chat_ids or ([chat_id] if chat_id else [])
        self.allowed_chat_ids = {int(item) for item in allowed if int(item)}
        if chat_id:
            self.allowed_chat_ids.add(int(chat_id))
        self._conn_override = conn
        self._conn_override_thread_id = threading.get_ident() if isinstance(conn, sqlite3.Connection) else None
        self.app: Application | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._thread: threading.Thread | None = None

    def _persist_conn(self) -> sqlite3.Connection | None:
        """Test-injected connection for outbound message persistence only."""
        return self._conn_override if isinstance(self._conn_override, sqlite3.Connection) else None

    def scoped_db_write(self):
        """Get a scoped write connection. Use in ``with`` block."""
        if self._conn_override is not None:
            return _conn_override_context(self._conn_override, self._conn_override_thread_id)
        from src.db.connection import scoped_write

        return scoped_write()

    def scoped_db_read(self):
        """Get a scoped read connection. Use in ``with`` block."""
        if self._conn_override is not None:
            return _conn_override_context(self._conn_override, self._conn_override_thread_id)
        from src.db.connection import scoped_read

        return scoped_read()

    def _build_application(self) -> Application:
        """Build Telegram clients without inheriting ambient process proxies.

        Telegram connectivity is deployment-critical and must not depend on proxy
        variables left behind by an interactive launcher shell. Explicit proxy
        support, if needed later, should be configuration-driven.
        """
        request_options = {"httpx_kwargs": {"trust_env": False}}
        return (
            Application.builder()
            .token(self.token)
            .request(HTTPXRequest(**request_options))
            .get_updates_request(HTTPXRequest(**request_options))
            .build()
        )

    async def _start(self) -> None:
        self.app = self._build_application()
        self._register_handlers()
        await self.app.initialize()
        await self.app.bot.set_my_commands(build_telegram_command_menu())

        from src.telegram.scheduler import register_scheduled_jobs

        register_scheduled_jobs(self.app, self.chat_id)
        # Warm wiki TF-IDF index so first agent wiki_search is not cold
        try:
            import logging
            from src.wiki.indexer import warm_wiki_index
            n = await asyncio.to_thread(warm_wiki_index)
            logging.getLogger(__name__).info("Warmed wiki index: %d chunks", n)
        except Exception:
            import logging
            logging.getLogger(__name__).debug("Wiki index warm failed", exc_info=True)
        await self.app.start()
        await self.app.updater.start_polling(drop_pending_updates=True)

    async def _stop(self) -> None:
        if self.app:
            await self.app.updater.stop()
            await self.app.stop()
            await self.app.shutdown()

    def start_in_thread(self) -> None:
        self._thread = threading.Thread(target=self._run_loop, daemon=True, name="tg-bot")
        self._thread.start()

    def _run_loop(self) -> None:
        import time as _time
        import logging as _logging
        _logger = _logging.getLogger(__name__)
        retry_delay = 5
        while True:
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            self._loop = loop
            try:
                loop.run_until_complete(self._start())
                loop.run_forever()
                loop.run_until_complete(self._stop())
                loop.close()
                break  # clean shutdown requested
            except Exception:
                _logger.exception(
                    "Bot connection failed — retrying in %ds…", retry_delay
                )
                self.app = None
                try:
                    loop.run_until_complete(self._stop())
                except Exception:
                    pass
                try:
                    loop.close()
                except Exception:
                    pass
                _time.sleep(retry_delay)
                retry_delay = min(retry_delay * 2, 60)

    def stop(self) -> None:
        if self._loop and self._loop.is_running():
            self._loop.call_soon_threadsafe(self._loop.stop)

    def _auth(self, update: Update) -> bool:
        if not update.effective_chat:
            return False
        if self.allow_all_chats:
            return True
        return update.effective_chat.id in self.allowed_chat_ids

    def _bound_command(self, handler, *, permission: str | None = None):
        """Check actual Telegram identity before entering a mixed/write command."""
        @wraps(handler)
        async def guarded(update: Update, context) -> None:
            if not self._auth(update):
                return
            try:
                with self.scoped_db_read() as conn:
                    actor = resolve_telegram_operator(
                        conn, telegram_user_id_from_update(update),
                    )
                    bound = actor.bound
                    allowed = False
                    if bound and permission:
                        from src.auth.repository import get_user
                        user = get_user(conn, actor.operator_id)
                        allowed = bool(
                            user and user["status"] == "active"
                            and permission in permissions_for_role(user["role"])
                        )
            except Exception:
                import logging as _logging
                _logging.getLogger(__name__).exception("Telegram command identity check failed")
                bound = False
            if not bound:
                await self._safe_reply(
                    update,
                    "Command not executed. Link your Telegram account to an active user "
                    "before using this command. Read-only commands such as /status remain available.",
                    message_kind="command_reply",
                )
                return
            if permission and not allowed:
                await self._safe_reply(
                    update, f"Command not executed. Permission required: {permission}.",
                    message_kind="command_reply",
                )
                return
            await handler(update, context)

        return guarded

    def _register_handlers(self) -> None:
        assert self.app is not None
        for cmd, handler in [
            ("start", self._cmd_start),
            ("scan", self._cmd_scan),
            ("portfolio", self._cmd_scan),
            ("status", self._cmd_status),
            ("session", self._cmd_session),
            ("pending", self._cmd_pending),
            ("approve", self._cmd_approve),
            ("reject", self._cmd_reject),
            ("brief", self._cmd_brief),
            ("note", self._cmd_note),
            ("track", self._cmd_track),
            ("close", self._cmd_close),
            ("reopen", self._cmd_reopen),
            ("flag", self._cmd_flag),
            ("query", self._cmd_query),
            ("newproject", self._cmd_newproject),
            ("report", self._cmd_report),
            ("batch", self._cmd_batch),
            ("signal_positive", self._cmd_signal_positive),
            ("skip", self._cmd_skip),
            ("reply", self._cmd_reply),
            ("activity", self._cmd_activity),
            ("set_preference", self._cmd_set_preference),
            ("delete_preference", self._cmd_delete_preference),
            ("ticket", self._cmd_ticket),
            ("support", self._cmd_support),
            ("resolve", self._cmd_resolve),
            ("notification", self._cmd_notification),
            ("thinking", self._cmd_thinking),
            ("lang", self._cmd_lang),
            ("help", self._cmd_help),
        ]:
            permission = (
                P_BUSINESS_WRITE if cmd in _BUSINESS_WRITE_COMMANDS else
                P_PROJECT_CLOSE if cmd in _PROJECT_CLOSE_COMMANDS else None
            )
            callback = (
                handler if cmd in _READ_ONLY_COMMANDS else
                self._bound_command(handler, permission=permission)
            )
            self.app.add_handler(CommandHandler(cmd, callback))
        self.app.add_handler(CallbackQueryHandler(self._on_callback))
        self.app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, self._on_text))
        self.app.add_error_handler(self._global_error_handler)

    @staticmethod
    async def _global_error_handler(update, context) -> None:
        """Catch-all for unhandled exceptions in any handler."""
        import logging as _logging
        _logging.getLogger(__name__).exception(
            "Unhandled exception in handler (update=%s)", update, exc_info=context.error,
        )
        # Best-effort reply so the user doesn't see silence
        if update and getattr(update, "effective_message", None):
            try:
                await update.effective_message.reply_text(
                    "Something went wrong processing that command. Please try again."
                )
            except Exception:
                _logging.getLogger(__name__).debug("Error handler reply also failed", exc_info=True)

    def _persist_command(self, update: Update, *, command_name: str, project_id: str | None = None) -> None:
        if not update.message or not update.effective_chat:
            return
        try:
            with self.scoped_db_write() as conn:
                from src.telegram.operator_binding import (
                    resolve_telegram_operator,
                    telegram_user_id_from_update,
                )

                telegram_user_id = telegram_user_id_from_update(update)
                actor = resolve_telegram_operator(conn, telegram_user_id)
                record_inbound_message(
                    conn,
                    chat_id=update.effective_chat.id,
                    telegram_message_id=update.message.message_id,
                    text=update.message.text,
                    message_kind="command",
                    operator_id=actor.operator_id,
                    reply_to_telegram_message_id=(
                        update.message.reply_to_message.message_id if update.message.reply_to_message else None
                    ),
                    project_id=project_id,
                    metadata={"command": command_name, "telegram_user_id": telegram_user_id},
                )
        except Exception:
            import logging as _logging
            _logging.getLogger(__name__).exception(
                "Failed to persist command /%s — continuing to handle it", command_name,
            )

    def _build_progress_streamer(
        self,
        *,
        chat_id: int | None,
        reply_to_message_id: int | None = None,
        project_id: str | None = None,
        session_id: str | None = None,
    ) -> TelegramProgressStreamer | None:
        if not self.app or chat_id is None:
            return None
        return TelegramProgressStreamer(
            self.app.bot,
            None,
            chat_id=chat_id,
            reply_to_message_id=reply_to_message_id,
            project_id=project_id,
            session_id=session_id,
        )

    def _build_thinking_tracker(
        self,
        *,
        chat_id: int | None,
        reply_to_message_id: int | None = None,
        message_thread_id: int | None = None,
        project_id: str | None = None,
        session_id: str | None = None,
    ):
        if not self.app or chat_id is None:
            return None
        from config.settings import CONFIG
        from src.telegram.thinking_tracker import ThinkingTracker, ThinkingTrackerConfig

        from src.repositories.operator_state_repo import get_thinking_window_enabled

        tcfg = CONFIG.telegram.thinking
        with self.scoped_db_read() as conn:
            thinking_enabled = get_thinking_window_enabled(conn)
        if not tcfg.enabled or not thinking_enabled:
            return None
        return ThinkingTracker(
            self.app.bot,
            self._persist_conn(),
            chat_id=chat_id,
            reply_to_message_id=reply_to_message_id,
            message_thread_id=message_thread_id,
            project_id=project_id,
            session_id=session_id,
            config=ThinkingTrackerConfig(
                enabled=tcfg.enabled,
                edit_interval=tcfg.edit_interval,
                fast_delete_seconds=tcfg.fast_delete_seconds,
                slow_keep_seconds=tcfg.slow_keep_seconds,
                disable_notification=tcfg.disable_notification,
            ),
        )

    def _build_progress_callback(
        self,
        streamer: TelegramProgressStreamer | None,
        thinking=None,
    ) -> ProgressCallback | None:
        return self._build_composite_progress_callback(streamer, thinking)

    def _build_composite_progress_callback(
        self,
        streamer: TelegramProgressStreamer | None,
        thinking=None,
    ) -> ProgressCallback | None:
        if streamer is None and thinking is None:
            return None
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return None

        streamer_methods = {
            "running": streamer.start_step if streamer else None,
            "done": streamer.complete_step if streamer else None,
            "error": streamer.fail_step if streamer else None,
        }

        def callback(step_name: str, status: str, detail: str | None = None) -> None:
            try:
                if thinking is not None and thinking.handles_step(step_name):
                    asyncio.run_coroutine_threadsafe(
                        thinking.on_progress(step_name, status, detail),
                        loop,
                    )
                    return
                method = streamer_methods.get(status)
                if method is None:
                    return
                asyncio.run_coroutine_threadsafe(method(step_name, detail), loop)
            except Exception:
                import logging as _cb_log
                _cb_log.getLogger(__name__).debug(
                    "Progress callback failed for step %s/%s", step_name, status, exc_info=True,
                )

        return callback

    def _build_stream_delta_callback(
        self,
        consumer: "StreamConsumer | None",
    ) -> "Callable[[str | None], None] | None":
        """Build a thread-safe delta callback that feeds a StreamConsumer.

        ``queue.Queue.put()`` is inherently thread-safe, so no
        ``asyncio.run_coroutine_threadsafe`` bridge is needed.
        """
        if consumer is None:
            return None
        return consumer.on_delta

    async def _safe_reply(
        self,
        update: Update,
        text: str,
        *,
        project_id: str | None = None,
        session_id: str | None = None,
        message_kind: str = "text",
        metadata: dict | None = None,
        object_links: list[dict] | None = None,
        reply_markup=None,
    ) -> None:
        if not update.message or not update.effective_chat:
            return
        await self._safe_send(
            update.effective_chat.id,
            text,
            reply_to_message_id=update.message.message_id,
            project_id=project_id,
            session_id=session_id,
            message_kind=message_kind,
            metadata=metadata,
            object_links=object_links,
            reply_markup=reply_markup,
        )

    async def _safe_send(
        self,
        chat_id: int,
        text: str,
        *,
        reply_to_message_id: int | None = None,
        project_id: str | None = None,
        session_id: str | None = None,
        message_kind: str = "text",
        metadata: dict | None = None,
        object_links: list[dict] | None = None,
        reply_markup=None,
    ) -> list[int]:
        if not self.app:
            return []
        from telegram.error import NetworkError, RetryAfter, TelegramError
        import logging as _logging
        _log = _logging.getLogger(__name__)
        try:
            return await send_persisted_message(
                self.app.bot,
                self._persist_conn(),
                chat_id=chat_id,
                text=text,
                reply_markup=reply_markup,
                reply_to_message_id=reply_to_message_id,
                project_id=project_id,
                session_id=session_id,
                message_kind=message_kind,
                metadata=metadata,
                object_links=object_links,
            )
        except RetryAfter as e:
            await asyncio.sleep(e.retry_after + 1)
            return await send_persisted_message(
                self.app.bot,
                self._persist_conn(),
                chat_id=chat_id,
                text=text,
                reply_markup=reply_markup,
                reply_to_message_id=reply_to_message_id,
                project_id=project_id,
                session_id=session_id,
                message_kind=message_kind,
                metadata=metadata,
                object_links=object_links,
            )
        except NetworkError as e:
            # Transient connectivity blip — wait 5s and retry once, then re-raise so
            # the caller's fallback (reply_text) can attempt direct delivery.
            _log.warning("_safe_send NetworkError for chat %s (%s) — retrying in 5s", chat_id, e)
            await asyncio.sleep(5)
            return await send_persisted_message(
                self.app.bot,
                self._persist_conn(),
                chat_id=chat_id,
                text=text,
                reply_markup=reply_markup,
                reply_to_message_id=reply_to_message_id,
                project_id=project_id,
                session_id=session_id,
                message_kind=message_kind,
                metadata=metadata,
                object_links=object_links,
            )
        except TelegramError:
            _log.exception("_safe_send TelegramError for chat %s", chat_id)
            return []

    async def push_card(self, card: InteractionCard, *, object_links: list[dict] | None = None) -> None:
        await self._safe_send(
            self.chat_id,
            format_card_text(card),
            project_id=card.project_id,
            message_kind="card",
            reply_markup=card_to_markup(card),
            object_links=object_links,
        )

    async def push_text(self, text: str, *, project_id: str | None = None) -> None:
        await self._safe_send(self.chat_id, text, project_id=project_id)


@contextmanager
def _conn_override_context(
    conn_override: sqlite3.Connection | object,
    override_thread_id: int | None,
) -> Iterator[sqlite3.Connection | object]:
    """Yield test-injected conn on the creating thread; else thread-local fallback."""
    if isinstance(conn_override, sqlite3.Connection):
        if override_thread_id == threading.get_ident():
            yield conn_override
            return
        from src.db.connection import get_connection

        yield get_connection()
        return
    yield conn_override


def _extract_project_id(conn: sqlite3.Connection, text: str) -> str | None:
    rows = conn.execute("SELECT project_id FROM projects").fetchall()
    text_upper = text.upper()
    for row in rows:
        project_id = dict(row)["project_id"]
        if project_id.upper() in text_upper:
            return project_id
    return None


def _chunk_text(text: str, max_len: int = 4000) -> list[str]:
    return chunk_text(text, max_len=max_len)


def create_bot(conn: sqlite3.Connection | None = None) -> TelegramBot:
    from config.settings import CONFIG

    token = CONFIG.telegram.bot_token
    chat_id = CONFIG.telegram.chat_id
    allowed_chat_ids = list(getattr(CONFIG.telegram, "allowed_chat_ids", []) or [])
    allow_all_chats = bool(getattr(CONFIG.telegram, "allow_all_chats", False))
    if not token:
        raise ValueError("telegram.bot_token not configured in config.json")
    if not chat_id and not allowed_chat_ids:
        raise ValueError("telegram.chat_id or telegram.allowed_chat_ids not configured in config.json")
    if not chat_id and allowed_chat_ids:
        chat_id = int(allowed_chat_ids[0])
    return TelegramBot(
        token=token,
        chat_id=chat_id,
        conn=conn,
        allowed_chat_ids=allowed_chat_ids,
        allow_all_chats=allow_all_chats,
    )
