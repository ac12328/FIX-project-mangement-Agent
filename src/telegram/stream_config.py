"""Configuration for Telegram LLM stream consumers."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from config.settings import TelegramConfig


@dataclass(frozen=True)
class StreamConsumerConfig:
    edit_interval: float = 0.8
    buffer_threshold: int = 24
    cursor: str = " ▉"
    transport: str = "auto"  # edit | draft | auto
    # HTML is the stable default; Rich Messages remain an explicit option.
    format_mode: str = "html"  # rich | html | plain
    fresh_final_after_seconds: float = 0.0
    max_message_length: int = 4096
    max_rich_message_length: int = 32768
    typing_refresh_sec: float = 4.0
    max_flood_strikes: int = 3
    max_edit_interval: float = 10.0


def config_from_settings(telegram: "TelegramConfig") -> StreamConsumerConfig:
    streaming = getattr(telegram, "streaming", None)
    if streaming is None:
        return StreamConsumerConfig()
    return StreamConsumerConfig(
        edit_interval=streaming.edit_interval,
        buffer_threshold=streaming.buffer_threshold,
        cursor=streaming.cursor,
        transport=streaming.transport,
        format_mode=getattr(streaming, "format_mode", "html"),
        fresh_final_after_seconds=streaming.fresh_final_after_seconds,
        max_message_length=streaming.max_message_length,
        max_rich_message_length=getattr(streaming, "max_rich_message_length", 32768),
        typing_refresh_sec=streaming.typing_refresh_sec,
    )