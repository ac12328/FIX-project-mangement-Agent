"""Compact tool results for orchestrator host context (ToolDigest)."""

from __future__ import annotations

import json
import sqlite3
from dataclasses import asdict, dataclass, field
from typing import Any

MAX_SUMMARY_CHARS = 800
OFFLOAD_THRESHOLD_CHARS = 2000

_DIGEST_TOOLS = frozenset({
    "wiki_read",
    "wiki_search",
    "search_emails",
    "get_email_body",
    "get_project_emails",
    "load_project_context",
    "list_reference_docs",
    "read_reference_doc",
    "get_ticket_evidence",
    "search_ticket_evidence",
    "search_knowledge",
    "delegate",
    "session_search",
})


@dataclass
class ToolDigest:
    tool_name: str
    ok: bool
    summary: str
    artifact_id: str | None = None
    citations: list[str] = field(default_factory=list)
    structured: dict[str, Any] | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), ensure_ascii=False)


def _truncate(text: str, limit: int = MAX_SUMMARY_CHARS) -> str:
    text = (text or "").strip()
    if len(text) <= limit:
        return text
    return text[: limit - 3] + "..."


def _error_summary(raw: dict[str, Any]) -> str:
    err = raw.get("error") or raw.get("text") or "tool failed"
    return _truncate(str(err))


def _digest_wiki_read(raw: dict[str, Any]) -> ToolDigest:
    path = raw.get("path", "")
    title = raw.get("title", "")
    body = raw.get("body") or ""
    citation = f"wiki:{path}" if path else ""
    summary = _truncate(f"{title}: {body}" if title else body)
    return ToolDigest(
        tool_name="wiki_read",
        ok="error" not in raw,
        summary=summary if "error" not in raw else _error_summary(raw),
        citations=[citation] if citation else [],
        structured={"path": path, "title": title, "body_chars": len(body)},
    )


def _digest_wiki_search(raw: dict[str, Any]) -> ToolDigest:
    structured_hits = raw.get("hits")
    results_blob = raw.get("results")
    lines: list[str] = []
    citations: list[str] = []

    if isinstance(structured_hits, list) and structured_hits:
        for hit in structured_hits[:5]:
            if not isinstance(hit, dict):
                continue
            path = hit.get("path") or hit.get("file") or ""
            title = hit.get("title") or path
            conf = hit.get("confidence") or "?"
            status = hit.get("status") or "?"
            summary = (hit.get("summary") or "").strip()
            line = f"- [{status}/{conf}] {title}"
            if path:
                line += f" (`{path}`)"
                citations.append(f"wiki:{path}")
            if summary:
                line += f" — {summary[:120]}"
            lines.append(line)
        summary_text = _truncate("\n".join(lines))
        return ToolDigest(
            tool_name="wiki_search",
            ok=True,
            summary=summary_text,
            citations=citations,
            structured={
                "query": raw.get("query"),
                "expanded_query": raw.get("expanded_query"),
                "hit_count": len(structured_hits),
                "intents": raw.get("intents") or [],
            },
        )

    if isinstance(results_blob, str):
        summary = _truncate(results_blob)
        return ToolDigest(
            tool_name="wiki_search",
            ok=True,
            summary=summary,
            citations=citations,
            structured={"query": raw.get("query")},
        )

    hits = results_blob or []
    for hit in hits[:5] if isinstance(hits, list) else []:
        if isinstance(hit, dict):
            path = hit.get("path") or hit.get("id") or ""
            title = hit.get("title") or path
            lines.append(f"- {title}")
            if path:
                citations.append(f"wiki:{path}")
    summary = _truncate("\n".join(lines) or json.dumps(raw, ensure_ascii=False)[:400])
    return ToolDigest(
        tool_name="wiki_search",
        ok=True,
        summary=summary,
        citations=citations,
        structured={"hit_count": len(hits) if isinstance(hits, list) else 0},
    )


def _digest_search_emails(raw: dict[str, Any]) -> ToolDigest:
    emails = raw.get("emails") or []
    total = raw.get("total_count", len(emails))
    try:
        total_n = int(total) if total is not None else len(emails)
    except (TypeError, ValueError):
        total_n = len(emails)
    lines: list[str] = [f"total_count={total_n}"]
    citations: list[str] = []
    for row in emails[:8]:
        if not isinstance(row, dict):
            continue
        eid = row.get("email_id") or ""
        subj = row.get("subject") or eid
        sender = row.get("sender_email") or ""
        cc = str(row.get("cc") or "").strip()
        when = (row.get("processed_at") or row.get("email_date") or "")[:10]
        preview = (row.get("body_preview") or row.get("summary") or "")[:120]
        line = f"- [{when}] {sender}: {subj}"
        if cc:
            line += f" | CC: {cc[:280]}"
        if preview:
            line += f" | {preview}"
        if eid:
            line += f" (id={eid})"
            citations.append(f"email:{eid}")
        lines.append(line)
    if total_n == 0:
        summary = "total_count=0 — no emails matched (retry with keyword=project_id if unexpected)"
    else:
        summary = _truncate("\n".join(lines), limit=1200)
    return ToolDigest(
        tool_name="search_emails",
        ok=True,
        summary=summary,
        citations=citations,
        structured={"total_count": total_n},
    )


def _digest_email_body(raw: dict[str, Any]) -> ToolDigest:
    eid = raw.get("email_id") or raw.get("reply_id") or ""
    subj = raw.get("subject") or ""
    sender = raw.get("sender_email") or ""
    direction = raw.get("direction") or ""
    body = raw.get("body_text") or raw.get("body") or raw.get("text") or ""
    if "error" in raw:
        summary = _error_summary(raw)
    else:
        header = " | ".join(part for part in (sender, direction, subj) if part)
        cc = str(raw.get("cc") or "").strip()
        if cc:
            header = f"{header}\nCC: {cc}" if header else f"CC: {cc}"
        summary = _truncate(f"{header}\n{body}" if header else body, limit=1200)
    cites = [f"email:{eid}"] if eid else []
    return ToolDigest(
        tool_name="get_email_body",
        ok="error" not in raw,
        summary=summary,
        citations=cites,
        structured={
            "email_id": raw.get("email_id"),
            "reply_id": raw.get("reply_id"),
            "offset": raw.get("offset", 0),
        },
    )


def _digest_reference_doc(raw: dict[str, Any]) -> ToolDigest:
    text = raw.get("text") or ""
    doc_key = raw.get("doc_key") or ""
    return ToolDigest(
        tool_name="read_reference_doc",
        ok=bool(text) and "error" not in str(text).lower()[:80],
        summary=_truncate(text),
        citations=[f"ref:{doc_key}"] if doc_key else [],
        structured={"truncated": raw.get("truncated")},
    )


def _digest_ticket_evidence(raw: dict[str, Any]) -> ToolDigest:
    text = raw.get("text") or raw.get("summary") or ""
    ticket = raw.get("ticket_id") or ""
    return ToolDigest(
        tool_name="get_ticket_evidence",
        ok=bool(text),
        summary=_truncate(text),
        citations=[f"ticket:{ticket}"] if ticket else [],
    )


def _digest_delegate(raw: dict[str, Any]) -> ToolDigest:
    summary = str(raw.get("summary") or "")
    findings = raw.get("findings") or []
    citations: list[str] = []
    finding_lines: list[str] = []
    if isinstance(findings, list):
        for f in findings[:8]:
            if isinstance(f, dict):
                if f.get("citation"):
                    citations.append(str(f["citation"]))
                bit = f.get("text") or f.get("fact") or f.get("detail") or str(f)
                finding_lines.append(f"- {bit}")
            else:
                finding_lines.append(f"- {f}")
    # Fold findings into summary so host does not lose email facts after digest
    if finding_lines:
        summary = (summary + "\nFindings:\n" + "\n".join(finding_lines)).strip()
    sources = raw.get("sources") or []
    if isinstance(sources, list):
        for s in sources[:8]:
            citations.append(str(s))
    ok = raw.get("ok", True)
    if raw.get("error"):
        ok = False
        error_summary = _error_summary(raw)
        if summary or finding_lines:
            summary = f"{summary}\nWarning: {error_summary}".strip()
        else:
            summary = error_summary
    session_id = raw.get("session_id")
    if session_id and session_id not in (summary or ""):
        summary = (
            f"{summary or 'delegate completed'}\n"
            f"session_id={session_id} (use resume_from for continuation)"
        ).strip()
    return ToolDigest(
        tool_name="delegate",
        ok=bool(ok),
        summary=_truncate(summary or "delegate completed", limit=1500),
        citations=citations,
        structured={
            "specialist_type": raw.get("specialist_type"),
            "skill_id": raw.get("skill_id"),
            "session_id": session_id,
            "completion_status": raw.get("completion_status"),
            "outcome": raw.get("outcome"),
            "committed": bool(raw.get("committed")),
            "operation_id": raw.get("operation_id"),
            "error": raw.get("error"),
            "finding_count": len(findings) if isinstance(findings, list) else 0,
        },
    )


def _digest_generic(tool_name: str, raw: dict[str, Any]) -> ToolDigest:
    if raw.get("error"):
        return ToolDigest(tool_name=tool_name, ok=False, summary=_error_summary(raw))
    text = raw.get("text") or raw.get("summary")
    if text:
        return ToolDigest(tool_name=tool_name, ok=True, summary=_truncate(str(text)))
    compact = _truncate(json.dumps(raw, ensure_ascii=False, default=str))
    return ToolDigest(tool_name=tool_name, ok=True, summary=compact)


def build_digest(tool_name: str, raw: Any) -> ToolDigest:
    """Build a ToolDigest from a handler result (dict or error)."""
    if not isinstance(raw, dict):
        return ToolDigest(
            tool_name=tool_name,
            ok=True,
            summary=_truncate(str(raw)),
        )
    if tool_name == "wiki_read":
        return _digest_wiki_read(raw)
    if tool_name == "wiki_search":
        return _digest_wiki_search(raw)
    if tool_name in ("search_emails", "get_project_emails"):
        return _digest_search_emails(raw)
    if tool_name == "get_email_body":
        return _digest_email_body(raw)
    if tool_name == "read_reference_doc":
        return _digest_reference_doc(raw)
    if tool_name == "get_ticket_evidence":
        return _digest_ticket_evidence(raw)
    if tool_name == "delegate":
        return _digest_delegate(raw)
    return _digest_generic(tool_name, raw)


def maybe_offload_raw(
    conn: sqlite3.Connection | None,
    *,
    operator_id: str | None,
    thread_id: str | None,
    tool_name: str,
    raw: Any,
    project_id: str | None = None,
) -> str | None:
    """Store full raw JSON; return artifact_id if offloaded."""
    if conn is None or not operator_id or not thread_id:
        return None
    payload = json.dumps(raw, ensure_ascii=False, default=str)
    if len(payload) <= OFFLOAD_THRESHOLD_CHARS:
        return None
    from src.memory.thread_artifacts import store_artifact

    return store_artifact(
        conn,
        operator_id=operator_id,
        thread_id=thread_id,
        tool_name=tool_name,
        content=payload,
        project_id=project_id,
    )


def digest_tool_result(
    tool_name: str,
    raw: Any,
    *,
    conn: sqlite3.Connection | None = None,
    operator_id: str | None = None,
    thread_id: str | None = None,
    project_id: str | None = None,
) -> dict[str, Any]:
    """Return ToolDigest dict for host tool messages; offload large raw payloads."""
    if tool_name not in _DIGEST_TOOLS:
        if isinstance(raw, dict):
            return raw
        return {"result": raw}

    digest = build_digest(tool_name, raw if isinstance(raw, dict) else {"text": str(raw)})
    artifact_id = maybe_offload_raw(
        conn,
        operator_id=operator_id,
        thread_id=thread_id,
        tool_name=tool_name,
        raw=raw,
        project_id=project_id,
    )
    if artifact_id:
        digest.artifact_id = artifact_id
    return digest.to_dict()


def should_digest_tool(tool_name: str) -> bool:
    return tool_name in _DIGEST_TOOLS