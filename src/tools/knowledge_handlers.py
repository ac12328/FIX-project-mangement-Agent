"""Handlers for on-demand knowledge tools."""

from __future__ import annotations

import sqlite3
from typing import Any

from src.email.reference_library import read_reference_doc_text


def chat_read_reference_doc(conn: sqlite3.Connection, args: dict) -> dict:
    # conn unused — allowlisted filesystem read only
    doc_key = (args.get("doc_key") or "").strip()
    if not doc_key:
        return {"text": "doc_key is required. Call list_reference_docs first."}
    max_chars = int(args.get("max_chars") or 12_000)
    result = read_reference_doc_text(doc_key, max_chars=max(500, min(max_chars, 32_000)))
    if not result.get("ok"):
        return {"text": result.get("error", "Could not read reference document.")}
    header = f"**{result['filename']}** ({doc_key})\n{result.get('description', '')}\n\n"
    return {
        "text": header + (result.get("text") or ""),
        "doc_key": doc_key,
        "truncated": result.get("truncated", False),
    }


KNOWLEDGE_TOOL_HANDLERS: dict[str, Any] = {
    "read_reference_doc": chat_read_reference_doc,
}