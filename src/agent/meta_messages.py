"""In-place update of harness meta messages to avoid message-list bloat."""

from __future__ import annotations


def upsert_meta_message(
    messages: list[dict],
    new_msg: dict,
    *,
    marker: str,
) -> list[dict]:
    """Replace a singleton meta message and remove stale duplicates."""
    matching_indexes = [
        index
        for index, message in enumerate(messages)
        if marker in str(message.get("content") or "")
    ]
    if not matching_indexes:
        messages.append(new_msg)
        return messages

    replacement_index = matching_indexes[-1]
    messages[:] = [
        new_msg if index == replacement_index else message
        for index, message in enumerate(messages)
        if index == replacement_index or index not in matching_indexes
    ]
    return messages
