"""
FreetextMixin: handles free-text operator messages and LLM agent integration.
Mixed into TelegramBot in bot.py.
"""

from __future__ import annotations

from typing import Callable

from telegram import Update

from src.llm.client import get_trace_ids, reset_trace_context
from src.observability.progress import ProgressCallback
from src.telegram.agent_chat import AgentChatResult, agent_loop
from src.telegram.persistence import record_inbound_message
from src.telegram.stream_consumer import StreamConsumer


class FreetextMixin:
    async def _on_text(self, update: Update, context) -> None:
        if not self._auth(update) or not update.message:
            return
        user_text = update.message.text.strip()
        if not user_text:
            return

        # Inject reply-to context so the LLM knows what "yes" / short
        # replies refer to when the operator uses Telegram's reply feature.
        reply_msg = update.message.reply_to_message
        if reply_msg and reply_msg.text:
            user_text = (
                f'[Replying to bot message: "{reply_msg.text[:500]}"]\n\n'
                f"{user_text}"
            )

        # Immediate "typing..." indicator — user sees bot is working
        try:
            from telegram.constants import ChatAction
            await update.effective_chat.send_action(ChatAction.TYPING)
        except Exception:
            pass

        # Pre-flight: persist inbound message using a scoped write connection
        # so that no write transaction is held open during the LLM loop.
        from logging import getLogger
        from src.telegram.operator_binding import (
            fallback_operator_id,
            resolve_telegram_operator,
            telegram_user_id_from_update,
        )

        _log = getLogger(__name__)
        telegram_user_id = telegram_user_id_from_update(update)
        operator_id = fallback_operator_id()
        actor_bound = False
        try:
            from src.repositories.operator_state_repo import record_telegram_interaction
            with self.scoped_db_write() as wconn:
                record_telegram_interaction(wconn)
                actor = resolve_telegram_operator(wconn, telegram_user_id)
                record_inbound_message(
                    wconn,
                    chat_id=update.effective_chat.id,
                    telegram_message_id=update.message.message_id,
                    text=user_text,
                    message_kind="text",
                    operator_id=actor.operator_id,
                    reply_to_telegram_message_id=(
                        update.message.reply_to_message.message_id if update.message.reply_to_message else None
                    ),
                    metadata={"telegram_user_id": telegram_user_id, "bound": actor.bound},
                )
            operator_id = actor.operator_id
            actor_bound = actor.bound
            # scoped_write auto-commits — no dangling write transaction
        except Exception:
            _log.exception("Pre-flight persistence failed — unbound turn will be denied")

        # A fallback operator is a read namespace, not an authenticated writer.
        # The free-text agent may invoke mutating shortcuts and tools, so an
        # unbound user must use explicit read-only commands until linked.
        if not actor_bound:
            await self._safe_reply(
                update,
                "未有執行或確認任何更新。請先將 Telegram 帳戶綁定至有效帳戶；"
                "如只需查閱資料，可使用 /status 或 /query。",
            )
            return

        try:
            if await self._handle_active_session(update, user_text):
                return
        except Exception:
            _log.exception("Session check failed — falling through to agent")

        # LLM-first: let the agent decide what to do with free text
        reset_trace_context()

        # Streaming consumer — delivers LLM text progressively via message edits.
        # When streaming is active, the stream IS the progress indicator (like
        # Hermes agent pattern) — no separate progress message needed.
        consumer = self._build_stream_consumer(
            chat_id=update.effective_chat.id,
            reply_to_message_id=update.message.message_id,
            chat_type=getattr(update.effective_chat, "type", None),
            message_thread_id=getattr(update.message, "message_thread_id", None),
        )
        on_stream_delta = self._build_stream_delta_callback(consumer)

        # Tool progress bubbles (Scheme A) — always on when configured, even with streaming.
        thinking = self._build_thinking_tracker(
            chat_id=update.effective_chat.id,
            reply_to_message_id=update.message.message_id,
            message_thread_id=getattr(update.message, "message_thread_id", None),
        )
        # High-level step streamer only when answer streaming is unavailable (tests, no bot).
        streamer = None
        if consumer is None:
            streamer = self._build_progress_streamer(
                chat_id=update.effective_chat.id,
                reply_to_message_id=update.message.message_id,
            )
        progress_cb = self._build_composite_progress_callback(streamer, thinking)

        # Start async consumer task (drains queue, edits Telegram message)
        import asyncio
        stream_task: asyncio.Task | None = None
        if consumer is not None:
            stream_task = asyncio.create_task(consumer.run())

        try:
            if streamer:
                await streamer.start_step("Receive instruction", "Telegram free-text message received")
                await streamer.complete_step("Receive instruction")
                await streamer.start_step("Classify intent", "Analyzing the message and selecting the workflow")
        except Exception:
            _log.exception("Progress streamer init failed — continuing without progress display")
            streamer = None

        reasoning_failed = False
        effective_project_id = None
        try:
            if streamer:
                await streamer.complete_step("Classify intent")
                await streamer.start_step("Run reasoning", "Preparing the answer")
            agent_result = await self._run_agent_respond(
                user_text, update.effective_chat.id,
                progress_cb=progress_cb, on_stream_delta=on_stream_delta,
                operator_id=operator_id,
            )
            response_text = agent_result.reply
            effective_project_id = agent_result.effective_project_id
        except Exception:
            from logging import getLogger
            getLogger(__name__).exception("Unhandled error while processing Telegram free text")
            if streamer:
                try:
                    await streamer.fail_step("Run reasoning", "Reasoning failed. Falling back to a safe error reply")
                except Exception:
                    _log.debug("streamer.fail_step also failed", exc_info=True)
            reasoning_failed = True
            response_text = (
                "處理訊息時發生內部錯誤。請重試或使用 /status 或 /query。"
            )

        # Signal consumer that streaming is done and wait for it to flush
        if consumer is not None:
            consumer.finish()
        if stream_task is not None:
            try:
                await asyncio.wait_for(stream_task, timeout=5.0)
            except (asyncio.TimeoutError, Exception):
                _log.debug("Stream consumer task did not finish cleanly", exc_info=True)

        if not response_text:
            response_text = "無法回覆此訊息。請重試或使用 /help。"

        trace_ids = get_trace_ids()
        metadata = {"llm_trace_ids": trace_ids} if trace_ids else None
        object_links = [
            {"object_type": "llm_trace", "object_ref": trace_id, "relation_type": "debug"}
            for trace_id in trace_ids
        ] or None

        # Finalize the streamed message FIRST — user sees clean text immediately.
        # Progress streamer updates (dismiss, etc.) follow afterward.
        # Draft transport leaves message_id unset until sendRichMessage/sendMessage;
        # still must finalize so the ephemeral draft is persisted.
        streamed = consumer is not None and consumer.should_finalize_stream
        stream_delivered = False
        if streamed:
            try:
                stream_delivered = await consumer.finalize(response_text)
                if stream_delivered:
                    await self._persist_streamed_reply(
                        chat_id=update.effective_chat.id,
                        consumer=consumer,
                        text=response_text,
                        reply_to_message_id=update.message.message_id,
                        project_id=effective_project_id if reasoning_failed is False else None,
                        metadata=metadata,
                        object_links=object_links,
                    )
            except Exception:
                _log.debug("Stream consumer finalize failed", exc_info=True)
                stream_delivered = False
            if not stream_delivered:
                _log.warning(
                    "Stream finalize incomplete for chat %s — falling back to full reply",
                    update.effective_chat.id,
                )
                streamed = False

        reply_markup = None

        try:
            if streamer:
                if not reasoning_failed:
                    await streamer.complete_step("Run reasoning")
                await streamer.start_step("Send final reply", "Delivering the final message")
        except Exception:
            _log.exception("Progress streamer update failed — continuing to send reply")

        if thinking is not None:
            try:
                await thinking.finalize()
            except Exception:
                _log.debug("ThinkingTracker finalize failed", exc_info=True)

        if streamed:
            pass  # Response already delivered via stream — nothing more to send
        else:
            # No streaming occurred — use existing send + dismiss flow
            try:
                await self._safe_reply(update, response_text, metadata=metadata, object_links=object_links, reply_markup=reply_markup)
            except Exception:
                from logging import getLogger
                getLogger(__name__).exception("_safe_reply failed; attempting plain fallback send")
                try:
                    await update.message.reply_text(response_text)
                except Exception:
                    getLogger(__name__).exception("Fallback reply also failed — user will see no response")
            if streamer:
                try:
                    await streamer.dismiss()
                except Exception:
                    _log.debug("Streamer dismiss failed", exc_info=True)

        if not reasoning_failed:
            try:
                from src.telegram.draft_tg_push import push_pending_draft_preview

                await push_pending_draft_preview(
                    scoped_db_read=self.scoped_db_read,
                    safe_send=self._safe_send,
                    chat_id=update.effective_chat.id,
                    reply_to_message_id=update.message.message_id,
                    user_text=user_text,
                    response_text=response_text,
                    project_id=effective_project_id,
                )
            except Exception:
                _log.exception("Auto draft preview push failed")

    def _build_stream_consumer(
        self,
        *,
        chat_id: int | None,
        reply_to_message_id: int | None = None,
        chat_type: str | None = None,
        message_thread_id: int | None = None,
    ) -> StreamConsumer | None:
        if not self.app or chat_id is None:
            return None
        from config.settings import CONFIG
        from src.telegram.stream_config import config_from_settings

        return StreamConsumer(
            self.app.bot,
            chat_id,
            config=config_from_settings(CONFIG.telegram),
            reply_to_message_id=reply_to_message_id,
            message_thread_id=message_thread_id,
            chat_type=chat_type,
        )

    async def _run_agent_respond(
        self,
        user_text: str,
        chat_id: int,
        *,
        progress_cb: ProgressCallback | None = None,
        on_stream_delta: "Callable[[str | None], None] | None" = None,
        operator_id: str | None = None,
    ) -> AgentChatResult:
        import asyncio
        from src.llm.watchdog import LLM_REASONING_TIMEOUT_SEC
        from src.agent.round_budget import MAX_AGENT_ROUNDS
        _AGENT_TIMEOUT = MAX_AGENT_ROUNDS * LLM_REASONING_TIMEOUT_SEC + 30
        if self._conn_override is None:
            return await asyncio.wait_for(
                asyncio.to_thread(
                    self._agent_respond_sync,
                    user_text,
                    chat_id,
                    progress_cb,
                    on_stream_delta,
                    operator_id,
                ),
                timeout=_AGENT_TIMEOUT,
            )
        return self._agent_respond_sync(
            user_text, chat_id, progress_cb, on_stream_delta, operator_id
        )

    def _agent_respond_sync(
        self,
        user_text: str,
        chat_id: int,
        progress_cb: ProgressCallback | None = None,
        on_stream_delta: "Callable[[str | None], None] | None" = None,
        operator_id: str | None = None,
    ) -> AgentChatResult:
        return agent_loop(
            None, user_text, chat_id,
            progress_cb=progress_cb, on_stream_delta=on_stream_delta,
            operator_id=operator_id,
            # Explicit actors here originate from the bound text preflight.
            require_persisted_actor=operator_id is not None,
        )

    async def _persist_streamed_reply(
        self,
        *,
        chat_id: int,
        consumer,
        text: str,
        reply_to_message_id: int | None,
        project_id: str | None,
        metadata: dict | None,
        object_links: list[dict] | None,
    ) -> None:
        if consumer is None or consumer.message_id is None or not text:
            return
        from src.telegram.persistence import record_outbound_message

        try:
            with self.scoped_db_write() as wconn:
                record_outbound_message(
                    wconn,
                    chat_id=chat_id,
                    telegram_message_id=consumer.message_id,
                    text=text,
                    message_kind="text",
                    reply_to_telegram_message_id=reply_to_message_id,
                    project_id=project_id,
                    metadata=metadata,
                    object_links=object_links,
                )
        except Exception:
            from logging import getLogger
            getLogger(__name__).debug(
                "Failed to persist streamed outbound reply", exc_info=True,
            )

    async def _run_agent_loop(
        self,
        instruction: str,
        *,
        chat_id: int = 0,
        progress_cb: ProgressCallback | None = None,
    ):
        import asyncio
        from src.telegram.agent_chat import agent_loop
        if self._conn_override is None:
            return await asyncio.to_thread(
                agent_loop, None, instruction, chat_id, progress_cb=progress_cb,
            )
        return agent_loop(None, instruction, chat_id, progress_cb=progress_cb)
