"""
Tool progress bubbles for Telegram — Scheme A from thinking-process design.

Shows per-tool status in a single editable message while the agent runs tools.
Works alongside StreamConsumer (answer streaming) as a separate progress bubble.
"""

from __future__ import annotations

import logging
import sqlite3
import time
from dataclasses import dataclass

from src.events.bus import Event, event_bus
from src.observability.tool_progress import (
    TOOL_STEP_PREFIX,
    format_tool_arg_preview,
    parse_sub_task_step,
    parse_tool_step,
)
from src.telegram.persistence import record_outbound_message
from src.telegram.runtime import _call_optional_async, chunk_text

logger = logging.getLogger(__name__)

# Re-export for tests
__all__ = [
    "ThinkingTracker",
    "ThinkingTrackerConfig",
    "tool_emoji",
    "format_tool_arg_preview",
    "parse_tool_step",
    "TOOL_STEP_PREFIX",
]

# (substring in tool name, emoji) — first match wins
_TOOL_EMOJI_RULES: list[tuple[str, str]] = [
    ("search", "🔍"),
    ("query", "🔍"),
    ("wiki", "📄"),
    ("draft", "📄"),
    ("email", "📨"),
    ("mail", "📨"),
    ("knowledge", "🔍"),
    ("update", "🗄️"),
    ("confirm", "🗄️"),
    ("note", "📝"),
    ("evidence", "📝"),
    ("followup", "📝"),
    ("contact", "📝"),
    ("think", "🤖"),
    ("delegate", "📋"),
    ("portfolio", "🗄️"),
    ("ticket", "🗄️"),
    ("support", "🗄️"),
]


@dataclass(frozen=True)
class ThinkingTrackerConfig:
    enabled: bool = True
    edit_interval: float = 1.5
    fast_delete_seconds: float = 2.0
    slow_keep_seconds: float = 5.0
    disable_notification: bool = True
    max_flood_strikes: int = 3


@dataclass
class _ToolState:
    tool_name: str
    status: str = "pending"  # pending | running | done | error
    started_at: float = 0.0
    finished_at: float | None = None
    param_preview: str | None = None
    error_detail: str | None = None
    sub_task: str | None = None
    sub_task_status: str = "pending"


def tool_emoji(tool_name: str) -> str:
    lowered = tool_name.lower()
    for needle, emoji in _TOOL_EMOJI_RULES:
        if needle in lowered:
            return emoji
    return "⚙️"


def _parse_preview_from_detail(detail: str | None) -> str | None:
    if not detail:
        return None
    if detail.startswith("preview="):
        return detail[8:].strip() or None
    return None


class ThinkingTracker:
    """Single-message tool progress bubble with rate-limited edits."""

    def __init__(
        self,
        bot,
        conn: sqlite3.Connection | None,
        *,
        chat_id: int,
        reply_to_message_id: int | None = None,
        message_thread_id: int | None = None,
        project_id: str | None = None,
        session_id: str | None = None,
        operator_id: str = "default_operator",
        config: ThinkingTrackerConfig | None = None,
    ) -> None:
        self.bot = bot
        self.conn = conn
        self.chat_id = chat_id
        self.reply_to_message_id = reply_to_message_id
        self.message_thread_id = message_thread_id
        self.project_id = project_id
        self.session_id = session_id
        self.operator_id = operator_id
        self.config = config or ThinkingTrackerConfig()

        self._tools: list[_ToolState] = []
        self._message_id: int | None = None
        self._last_rendered = ""
        self._last_edit_time = 0.0
        self._flood_strikes = 0
        self._edit_supported = True
        self._started = False

    def handles_step(self, step_name: str) -> bool:
        return (
            parse_tool_step(step_name) is not None
            or parse_sub_task_step(step_name) is not None
        )

    async def on_progress(
        self,
        step_name: str,
        status: str,
        detail: str | None = None,
    ) -> None:
        if not self.config.enabled:
            return
        sub_task_name = parse_sub_task_step(step_name)
        if sub_task_name is not None:
            delegate = self._find_running_delegate()
            if delegate is not None:
                delegate.sub_task = sub_task_name
                delegate.sub_task_status = status
            await self._flush(force=status in {"done", "error"})
            return

        tool_name = parse_tool_step(step_name)
        if tool_name is None:
            return

        state = self._find_or_create(tool_name)
        now = time.monotonic()

        if status == "running":
            state.status = "running"
            if state.started_at <= 0:
                state.started_at = now
            preview = _parse_preview_from_detail(detail)
            if preview:
                state.param_preview = preview
        elif status == "done":
            state.status = "done"
            state.finished_at = now
            if state.started_at <= 0:
                state.started_at = now
        elif status == "error":
            state.status = "error"
            state.finished_at = now
            if state.started_at <= 0:
                state.started_at = now
            if detail:
                state.error_detail = detail[:120]

        await self._flush(force=status in {"done", "error"})

    async def finalize(self) -> None:
        """Apply auto-delete rules after the agent loop completes."""
        if not self.config.enabled or self._message_id is None:
            self._tools.clear()
            return

        all_fast_success = (
            self._tools
            and all(
                t.status == "done"
                and t.finished_at is not None
                and t.started_at > 0
                and (t.finished_at - t.started_at) < self.config.fast_delete_seconds
                for t in self._tools
            )
        )
        any_error = any(t.status == "error" for t in self._tools)
        any_slow = any(
            t.status == "done"
            and t.finished_at is not None
            and t.started_at > 0
            and (t.finished_at - t.started_at) >= self.config.slow_keep_seconds
            for t in self._tools
        )

        if any_error or any_slow:
            await self._flush(force=True)
        elif all_fast_success:
            await self._delete_message()
        else:
            await self._flush(force=True)
            await self._delete_message()

        self._tools.clear()

    def _find_running_delegate(self) -> _ToolState | None:
        for state in reversed(self._tools):
            if state.tool_name == "delegate" and state.status == "running":
                return state
        return None

    def _find_or_create(self, tool_name: str) -> _ToolState:
        for state in self._tools:
            if state.tool_name == tool_name and state.status in {"pending", "running"}:
                return state
        state = _ToolState(tool_name=tool_name)
        self._tools.append(state)
        return state

    def render(self) -> str:
        if not self._tools:
            return "🔄 正在處理..."

        if len(self._tools) == 1:
            return self._render_single(self._tools[0])

        lines = ["🔄 正在處理..."]
        for index, tool in enumerate(self._tools):
            prefix = "└─" if index == len(self._tools) - 1 else "├─"
            lines.append(f"{prefix} {self._render_tool_line(tool)}")
        return "\n".join(lines)

    def _sub_task_line(self, tool: _ToolState) -> str | None:
        if not tool.sub_task:
            return None
        if tool.sub_task_status == "running":
            return f"└─ 📋 Sub-task: {tool.sub_task}"
        if tool.sub_task_status == "done":
            return f"└─ 📋 Sub-task: {tool.sub_task} ✅"
        if tool.sub_task_status == "error":
            return f"└─ 📋 Sub-task: {tool.sub_task} ❌"
        return f"└─ 📋 Sub-task: {tool.sub_task}"

    def _render_single(self, tool: _ToolState) -> str:
        emoji = tool_emoji(tool.tool_name)
        if tool.status == "running":
            lines = [f"{emoji} 正在調用 {tool.tool_name}..."]
            sub_line = self._sub_task_line(tool)
            if sub_line:
                lines.append(sub_line)
            elif tool.param_preview:
                lines.append(f"└─ 參數: {tool.param_preview}")
            return "\n".join(lines)
        if tool.status == "done":
            duration = self._duration(tool)
            line = f"✅ {tool.tool_name} 完成（{duration:.1f}s）"
            if tool.tool_name == "delegate":
                sub_line = self._sub_task_line(tool)
                if sub_line:
                    return f"{line}\n{sub_line}"
            return line
        if tool.status == "error":
            duration = self._duration(tool)
            line = f"❌ {tool.tool_name} 失敗（{duration:.1f}s）"
            if tool.error_detail:
                line += f"\n└─ {tool.error_detail}"
            return line
        return f"{emoji} 正在調用 {tool.tool_name}..."

    def _render_tool_line(self, tool: _ToolState) -> str:
        emoji = tool_emoji(tool.tool_name)
        if tool.status == "running":
            base = f"{emoji} {tool.tool_name:<22} ⏳ 進行中"
            sub_line = self._sub_task_line(tool)
            if sub_line and tool.tool_name == "delegate":
                return f"{base}\n   {sub_line}"
            return base
        if tool.status == "pending":
            return f"{emoji} {tool.tool_name:<22} ⏳ 等待中"
        if tool.status == "done":
            duration = self._duration(tool)
            return f"{emoji} {tool.tool_name:<22} ✅ 完成（{duration:.1f}s）"
        duration = self._duration(tool)
        return f"{emoji} {tool.tool_name:<22} ❌ 失敗（{duration:.1f}s）"

    @staticmethod
    def _duration(tool: _ToolState) -> float:
        if tool.finished_at is None or tool.started_at <= 0:
            return 0.0
        return max(0.0, tool.finished_at - tool.started_at)

    async def _flush(self, *, force: bool = False) -> None:
        text = self.render()
        if not text or (text == self._last_rendered and not force):
            return

        now = time.monotonic()
        if (
            not force
            and self._message_id is not None
            and (now - self._last_edit_time) < self.config.edit_interval
        ):
            return

        if self._message_id is None:
            await self._send_initial(text)
        elif self._edit_supported:
            ok = await self._edit(text)
            if not ok:
                return
        else:
            return

        self._last_rendered = text
        self._last_edit_time = now
        self._publish_event(text)

    async def _send_initial(self, text: str) -> None:
        content = chunk_text(text, max_len=4096)[0]
        kwargs: dict = {
            "chat_id": self.chat_id,
            "text": content,
            "reply_to_message_id": self.reply_to_message_id,
        }
        if self.message_thread_id is not None:
            kwargs["message_thread_id"] = self.message_thread_id
        if self.config.disable_notification:
            kwargs["disable_notification"] = True
        try:
            sent = await self.bot.send_message(**kwargs)
            self._message_id = getattr(sent, "message_id", None)
            self._started = True
            persist_kwargs = dict(
                chat_id=self.chat_id,
                telegram_message_id=self._message_id,
                text=content,
                message_kind="tool_progress",
                reply_to_telegram_message_id=self.reply_to_message_id,
                project_id=self.project_id,
                session_id=self.session_id,
                operator_id=self.operator_id,
                metadata={"thinking_tracker": True},
            )
            if self.conn is not None:
                record_outbound_message(self.conn, **persist_kwargs)
            else:
                from src.db.connection import scoped_write

                with scoped_write() as wconn:
                    record_outbound_message(wconn, **persist_kwargs)
        except Exception:
            logger.debug("ThinkingTracker initial send failed", exc_info=True)

    async def _edit(self, text: str) -> bool:
        if self._message_id is None:
            return False
        content = chunk_text(text, max_len=4096)[0]
        if content == self._last_rendered:
            return True
        try:
            kwargs: dict = {
                "chat_id": self.chat_id,
                "message_id": self._message_id,
                "text": content,
            }
            await _call_optional_async(
                getattr(self.bot, "edit_message_text", None),
                **kwargs,
            )
            return True
        except Exception as exc:
            return self._handle_edit_failure(exc)

    def _handle_edit_failure(self, exc: Exception) -> bool:
        err = str(exc).lower()
        if "message is not modified" in err:
            return True
        if any(token in err for token in ("flood", "retry", "too many")):
            self._flood_strikes += 1
            self._last_edit_time = time.monotonic() + self.config.edit_interval
            if self._flood_strikes >= self.config.max_flood_strikes:
                self._edit_supported = False
                logger.warning(
                    "ThinkingTracker edit disabled after %d flood strikes (chat=%s)",
                    self._flood_strikes,
                    self.chat_id,
                )
            return False
        logger.debug("ThinkingTracker edit failed", exc_info=True)
        return False

    async def _delete_message(self) -> None:
        if self._message_id is None:
            return
        msg_id = self._message_id
        self._message_id = None
        try:
            await self.bot.delete_message(chat_id=self.chat_id, message_id=msg_id)
        except Exception:
            logger.debug("ThinkingTracker delete failed (non-critical)", exc_info=True)

    def _publish_event(self, content: str) -> None:
        event_bus.publish(Event(
            event_type="tool_progress",
            project_id=self.project_id,
            surface="telegram",
            detail={
                "content": content,
                "message_id": self._message_id,
                "session_id": self.session_id,
            },
        ))