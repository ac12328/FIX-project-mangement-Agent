"""Deterministic email conversation grouping for PM read surfaces."""

from __future__ import annotations

import json
import re
import sqlite3
from dataclasses import dataclass
from typing import Any

from src.assistant.workspace_read_support import normalize_timestamp

_SUBJECT_PREFIX_RE = re.compile(r"^\s*(?:(?:re|fw|fwd)\s*:\s*)+", re.IGNORECASE)
_MESSAGE_ID_RE = re.compile(r"<([^<>]+)>")


@dataclass(frozen=True)
class ResolvedEmailThread:
    """A stable grouped conversation whose latest message determines state."""

    thread_key: str
    project_id: str
    messages: tuple[dict[str, Any], ...]
    latest: dict[str, Any]

    @property
    def candidate_unanswered(self) -> bool:
        return str(self.latest.get("direction") or "").lower() == "inbound"


class _DisjointSet:
    def __init__(self) -> None:
        self.parent: dict[str, str] = {}

    def add(self, value: str) -> None:
        self.parent.setdefault(value, value)

    def find(self, value: str) -> str:
        parent = self.parent[value]
        if parent != value:
            self.parent[value] = self.find(parent)
        return self.parent[value]

    def union(self, left: str, right: str) -> None:
        self.add(left)
        self.add(right)
        left_root = self.find(left)
        right_root = self.find(right)
        if left_root == right_root:
            return
        first, second = sorted((left_root, right_root))
        self.parent[second] = first


def normalize_message_id(value: str | None) -> str | None:
    normalized = str(value or "").strip()
    while len(normalized) >= 2 and normalized.startswith("<") and normalized.endswith(">"):
        normalized = normalized[1:-1].strip()
    return normalized.casefold() or None


def normalize_subject(value: str | None) -> str | None:
    normalized = _SUBJECT_PREFIX_RE.sub("", str(value or "")).strip()
    normalized = " ".join(normalized.split()).casefold()
    return normalized or None


def _reference_values(value: Any) -> list[str]:
    raw = str(value or "").strip()
    if not raw:
        return []
    values: list[str] = []
    try:
        parsed = json.loads(raw)
    except (TypeError, json.JSONDecodeError):
        parsed = None
    if isinstance(parsed, list):
        values.extend(str(item) for item in parsed)
    else:
        bracketed = _MESSAGE_ID_RE.findall(raw)
        values.extend(bracketed or re.split(r"[\s,;]+", raw))
    return [
        normalized
        for item in values
        if (normalized := normalize_message_id(item)) is not None
    ]


def _row_tokens(row: dict[str, Any]) -> list[str]:
    explicit = [
        normalize_message_id(row.get("email_id")),
        normalize_message_id(row.get("thread_fp")),
        normalize_message_id(row.get("thread_id")),
        normalize_message_id(row.get("in_reply_to")),
        *_reference_values(row.get("references_chain")),
    ]
    tokens = list(dict.fromkeys(item for item in explicit if item))
    has_conversation_metadata = any(
        row.get(field)
        for field in ("thread_fp", "thread_id", "in_reply_to", "references_chain")
    )
    if not has_conversation_metadata:
        subject = normalize_subject(row.get("subject"))
        if subject:
            tokens.append(f"subject:{subject}")
    return tokens


def resolve_project_email_threads(
    conn: sqlite3.Connection,
    project_id: str,
) -> list[ResolvedEmailThread]:
    """Group processed emails using explicit thread lineage and safe subject fallback."""
    rows = conn.execute(
        """SELECT email_id, project_id, processed_at, received_at, result,
                  thread_fp, subject, sender_email, direction, thread_id,
                  in_reply_to, references_chain, body_snippet
           FROM processed_emails
           WHERE project_id=?
           ORDER BY COALESCE(received_at, processed_at), email_id""",
        (project_id,),
    ).fetchall()
    if not rows:
        return []

    disjoint = _DisjointSet()
    normalized_rows: list[tuple[dict[str, Any], list[str]]] = []
    for raw in rows:
        item = dict(raw)
        item["direction"] = str(item.get("direction") or "").casefold()
        item["occurred_at"] = normalize_timestamp(
            item.get("received_at") or item.get("processed_at")
        )
        tokens = _row_tokens(item)
        if not tokens:
            tokens = [f"email:{str(item['email_id']).casefold()}"]
        for token in tokens:
            disjoint.add(token)
        for token in tokens[1:]:
            disjoint.union(tokens[0], token)
        normalized_rows.append((item, tokens))

    grouped: dict[str, list[dict[str, Any]]] = {}
    identifiers: dict[str, set[str]] = {}
    for item, tokens in normalized_rows:
        root = disjoint.find(tokens[0])
        grouped.setdefault(root, []).append(item)
        identifiers.setdefault(root, set()).update(tokens)

    threads: list[ResolvedEmailThread] = []
    for root, messages in grouped.items():
        messages.sort(
            key=lambda item: (
                str(item.get("occurred_at") or ""),
                str(item.get("email_id") or ""),
            )
        )
        latest = messages[-1]
        preferred = (
            normalize_message_id(latest.get("thread_fp"))
            or normalize_message_id(latest.get("thread_id"))
        )
        if not preferred:
            non_subject = sorted(
                token for token in identifiers[root] if not token.startswith("subject:")
            )
            preferred = non_subject[0] if non_subject else root.removeprefix("subject:")
        threads.append(
            ResolvedEmailThread(
                thread_key=preferred,
                project_id=project_id,
                messages=tuple(messages),
                latest=latest,
            )
        )

    threads.sort(
        key=lambda thread: (
            str(thread.latest.get("occurred_at") or ""),
            str(thread.latest.get("email_id") or ""),
        ),
        reverse=True,
    )
    return threads


def candidate_unanswered_threads(
    conn: sqlite3.Connection,
    project_id: str,
) -> list[ResolvedEmailThread]:
    """Return inbound-last conversations without claiming a response is required."""
    return [
        thread
        for thread in resolve_project_email_threads(conn, project_id)
        if thread.candidate_unanswered
    ]


def count_unanswered_threads(conn: sqlite3.Connection, project_id: str) -> int:
    """Count candidate unanswered conversations using the shared resolver."""
    return len(candidate_unanswered_threads(conn, project_id))
