"""Shared helpers for callback handler modules."""

from __future__ import annotations

import json


def extract_message_metadata(message_context: dict | None) -> dict:
    """Return parsed metadata from a Telegram message context row."""
    if not message_context:
        return {}
    raw = message_context.get("metadata")
    if raw is None:
        raw = message_context.get("metadata_json")
    if isinstance(raw, str):
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError:
            return {}
        return parsed if isinstance(parsed, dict) else {}
    if isinstance(raw, dict):
        return raw
    return {}


def extract_link(message_context: dict | None, object_type: str) -> dict | None:
    if not message_context:
        return None
    for link in message_context.get("links", []):
        if link.get("object_type") == object_type:
            return link
    return None


def extract_object_ref(message_context: dict | None) -> str | None:
    if not message_context or not message_context.get("links"):
        return None
    return message_context["links"][0]["object_ref"]
