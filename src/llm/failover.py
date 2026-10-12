"""Primary-to-backup LLM failover heuristics."""

from __future__ import annotations

from src.llm.watchdog import LLMTimeoutError


def should_failover_to_backup(error: Exception) -> bool:
    """Return True when the backup provider is worth trying.

    Connection/timeouts and malformed JSON from the primary proxy are retried
    on backup. Context-window overflows are not — the same prompt would fail
    there too.
    """
    if isinstance(error, LLMTimeoutError):
        return True

    err_type = type(error).__name__.lower()
    err_str = str(error).lower()

    if (
        "contextwindowexceeded" in err_type
        or "context_window" in err_str
        or "maximum context length" in err_str
        or "context length" in err_str
    ):
        return False

    if any(
        kw in err_str
        for kw in ("timeout", "connection", "unreachable", "refused", "reset", "eof")
    ):
        return True

    malformed_json_markers = (
        "unterminated string",
        "jsondecodeerror",
        "invalid json",
        "validation error",
        "expecting value",
        "expecting property",
        "decode error",
        "failed to parse",
        "malformed",
    )
    if any(marker in err_str for marker in malformed_json_markers):
        return True

    if "badrequesterror" in err_type or "bad request" in err_str:
        return True

    return False