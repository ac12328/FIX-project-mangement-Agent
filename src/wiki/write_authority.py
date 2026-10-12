"""Single authority for who may write wiki/ pages.

Locked product rule: only the wiki compiler and a human editor may
mutate markdown under wiki/. Agent tools and the email miner must not
create tip pages or enrich hubs directly.
"""
from __future__ import annotations

from typing import Final

WikiWriter = str

WRITER_COMPILER: Final = "compiler"
WRITER_HUMAN: Final = "human"
WRITER_AGENT: Final = "agent"
WRITER_MINER: Final = "miner"

ALLOWED_WIKI_WRITERS: Final[frozenset[str]] = frozenset({
    WRITER_COMPILER,
    WRITER_HUMAN,
})

WIKI_WRITER_BLOCKED_CODE: Final = "wiki_writer_not_allowed"


def wiki_write_allowed(writer: WikiWriter) -> bool:
    return (writer or "").strip() in ALLOWED_WIKI_WRITERS


def wiki_write_block_reason(writer: WikiWriter) -> str:
    return (
        f"Wiki writes from {writer or 'unknown'} are blocked; "
        "only compiler and human editors may write pages."
    )


def authorize_wiki_write(writer: WikiWriter) -> str | None:
    """Return a rejection reason, or None when the write may proceed."""
    if wiki_write_allowed(writer):
        return None
    return wiki_write_block_reason(writer)


def wiki_write_denied_payload(writer: WikiWriter, *, operation: str = "wiki_write") -> dict:
    """Receipt used by agent wiki tools when the writer is not allowed."""
    return {
        "success": False,
        "verified": False,
        "operation": operation,
        "code": WIKI_WRITER_BLOCKED_CODE,
        "error": wiki_write_block_reason(writer),
    }
