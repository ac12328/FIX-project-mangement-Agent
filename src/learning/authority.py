"""Exact observation provenance selectors shared by approval and retrieval."""
from __future__ import annotations

import hashlib
import json
import sqlite3
from typing import Literal

SelectorKind = Literal["source_id", "content_hash", "batch_id"]


def observation_selectors(row) -> dict[str, str]:
    payload = _json(row["raw_payload_json"])
    values = {
        "source_id": str(row["source_id"] or ""),
        "content_hash": str(row["content_hash"] or ""),
        "batch_id": str(payload.get("batch_id") or ""),
    }
    return {kind: value for kind, value in values.items() if value}


def authority_denied(conn: sqlite3.Connection, observation) -> str | None:
    for kind, value in observation_selectors(observation).items():
        if conn.execute(
            "SELECT 1 FROM memory_authority_denylist WHERE authority_kind=? AND authority_value=?",
            (kind, value),
        ).fetchone():
            return f"denied exact {kind} authority"
    return None


def selector_hash(kind: SelectorKind, value: str) -> str:
    return hashlib.sha256(f"{kind}|{value}".encode()).hexdigest()


def _json(value: str) -> dict:
    try:
        parsed = json.loads(value or "{}")
        return parsed if isinstance(parsed, dict) else {}
    except (TypeError, json.JSONDecodeError):
        return {}
