"""Compatibility facade for neutral conversation compression."""

from src.context.compression import (
    _IDENTITY_BLOCK,
    _format_messages_for_summary,
    _prepend_identity,
    compact_tool_result,
    compress_conversation,
    micro_compact_tool_results,
)

__all__ = [
    "_IDENTITY_BLOCK",
    "_format_messages_for_summary",
    "_prepend_identity",
    "compact_tool_result",
    "compress_conversation",
    "micro_compact_tool_results",
]
