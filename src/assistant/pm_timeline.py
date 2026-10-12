"""Timeline and unanswered-conversation read tools for the AI Workspace."""

from __future__ import annotations

import json
import math
import re
import sqlite3
from datetime import datetime
from typing import Any
from urllib.parse import quote

from src.assistant.workspace_read_support import (
    add_source,
    ensure_project_in_scope,
    normalize_timestamp,
    paginate_records,
    parse_as_of,
    parse_time_filter,
    visible_project_ids,
)
from src.portfolio.email_thread_state import resolve_project_email_threads

_TIMELINE_KINDS = frozenset({"email", "activity", "milestone", "evidence"})
_QUERY_TOKEN_RE = re.compile(r"[\w]+", re.UNICODE)


def _query_matches(searchable: str, query: str) -> bool:
    """Match concise natural-language queries without requiring an exact phrase."""
    if not query or query in searchable:
        return True
    terms = list(dict.fromkeys(_QUERY_TOKEN_RE.findall(query)))
    if not terms:
        return True
    matched = sum(1 for term in terms if term in searchable)
    minimum_matches = max(1, math.ceil(len(terms) * 0.6))
    return matched >= minimum_matches


def _project_exists(conn: sqlite3.Connection, project_id: str) -> bool:
    return conn.execute(
        "SELECT 1 FROM projects WHERE project_id=? AND project_id != '_system'",
        (project_id,),
    ).fetchone() is not None


def _email_timeline_rows(
    conn: sqlite3.Connection,
    project_id: str,
) -> list[dict[str, Any]]:
    threads = resolve_project_email_threads(conn, project_id)
    thread_by_email = {
        str(message["email_id"]): thread.thread_key
        for thread in threads
        for message in thread.messages
    }
    rows = conn.execute(
        """SELECT email_id, received_at, processed_at, subject, sender_email,
                  direction, thread_fp, thread_id, in_reply_to, references_chain,
                  result, body_snippet
           FROM processed_emails WHERE project_id=?""",
        (project_id,),
    ).fetchall()
    results: list[dict[str, Any]] = []
    for raw in rows:
        item = dict(raw)
        record_id = str(item["email_id"])
        results.append({
            "kind": "email",
            "record_id": record_id,
            "project_id": project_id,
            "occurred_at": normalize_timestamp(
                item.get("received_at") or item.get("processed_at")
            ),
            "title": str(item.get("subject") or "Email"),
            "summary": str(item.get("body_snippet") or "")[:1200],
            "direction": str(item.get("direction") or "").casefold() or None,
            "thread_key": thread_by_email.get(record_id),
            "metadata": {
                "sender_email": item.get("sender_email"),
                "result": item.get("result"),
                "thread_fp": item.get("thread_fp"),
                "thread_id": item.get("thread_id"),
                "in_reply_to": item.get("in_reply_to"),
            },
        })
    return results


def _activity_timeline_rows(
    conn: sqlite3.Connection,
    project_id: str,
) -> list[dict[str, Any]]:
    rows = conn.execute(
        """SELECT id, actor, surface, action_type, object_ref, summary,
                  metadata_json, created_at
           FROM activity_log WHERE project_id=?""",
        (project_id,),
    ).fetchall()
    return [{
        "kind": "activity",
        "record_id": str(row["id"]),
        "project_id": project_id,
        "occurred_at": normalize_timestamp(row["created_at"]),
        "title": str(row["action_type"] or "Activity"),
        "summary": str(row["summary"] or "")[:1200],
        "direction": None,
        "thread_key": None,
        "metadata": {
            "actor": row["actor"],
            "surface": row["surface"],
            "object_ref": row["object_ref"],
            "metadata": _safe_json(row["metadata_json"]),
        },
    } for row in rows]


def _milestone_timeline_rows(
    conn: sqlite3.Connection,
    project_id: str,
) -> list[dict[str, Any]]:
    rows = conn.execute(
        """SELECT id, from_milestone, to_milestone, changed_by, changed_at,
                  reason, evidence_ref, proposed_update_id
           FROM milestone_history WHERE project_id=?""",
        (project_id,),
    ).fetchall()
    return [{
        "kind": "milestone",
        "record_id": str(row["id"]),
        "project_id": project_id,
        "occurred_at": normalize_timestamp(row["changed_at"]),
        "title": f"Milestone changed to {row['to_milestone']}",
        "summary": str(row["reason"] or "")[:1200],
        "direction": None,
        "thread_key": None,
        "metadata": {
            "from_milestone": row["from_milestone"],
            "to_milestone": row["to_milestone"],
            "changed_by": row["changed_by"],
            "evidence_ref": row["evidence_ref"],
            "proposed_update_id": row["proposed_update_id"],
        },
    } for row in rows]


def _evidence_timeline_rows(
    conn: sqlite3.Connection,
    project_id: str,
) -> list[dict[str, Any]]:
    rows = conn.execute(
        """SELECT evidence_id, evidence_type, content_summary, content_text,
                  source_ref, evidence_at, created_at
           FROM evidence_store WHERE project_id=?""",
        (project_id,),
    ).fetchall()
    return [{
        "kind": "evidence",
        "record_id": str(row["evidence_id"]),
        "project_id": project_id,
        "occurred_at": normalize_timestamp(row["evidence_at"] or row["created_at"]),
        "title": f"{row['evidence_type']} evidence",
        "summary": str(row["content_summary"] or row["content_text"] or "")[:1200],
        "direction": None,
        "thread_key": None,
        "metadata": {
            "evidence_type": row["evidence_type"],
            "source_ref": row["source_ref"],
        },
    } for row in rows]


def _safe_json(value: Any) -> Any:
    if not value:
        return None
    try:
        return json.loads(str(value))
    except (TypeError, json.JSONDecodeError):
        return str(value)


def search_project_timeline(
    conn: sqlite3.Connection,
    args: dict[str, Any],
) -> dict[str, Any]:
    """Search a project's normalized email/activity/milestone/evidence timeline."""
    project_id = str(args.get("project_id") or "").strip()
    if not project_id:
        return {"error": "project_id is required"}
    ensure_project_in_scope(args, project_id)
    if not _project_exists(conn, project_id):
        return {"error": f"Project {project_id} not found"}

    raw_kinds = args.get("kinds") or sorted(_TIMELINE_KINDS)
    kinds = {str(item).strip().lower() for item in raw_kinds}
    invalid = sorted(kinds - _TIMELINE_KINDS)
    if invalid:
        return {"error": f"Unsupported timeline kinds: {', '.join(invalid)}"}
    try:
        from_at = parse_time_filter(args.get("from"))
        to_at = parse_time_filter(args.get("to"), end_of_day=True)
    except ValueError as exc:
        return {"error": str(exc)}
    if from_at and to_at and from_at > to_at:
        return {"error": "from must not be after to"}

    records: list[dict[str, Any]] = []
    if "email" in kinds:
        records.extend(_email_timeline_rows(conn, project_id))
    if "activity" in kinds:
        records.extend(_activity_timeline_rows(conn, project_id))
    if "milestone" in kinds:
        records.extend(_milestone_timeline_rows(conn, project_id))
    if "evidence" in kinds:
        records.extend(_evidence_timeline_rows(conn, project_id))

    query = str(args.get("query") or "").strip().casefold()
    direction = str(args.get("direction") or "").strip().casefold()
    thread_key = str(args.get("thread_id") or "").strip().casefold()
    as_of = parse_as_of(args.get("_assistant_as_of"))
    as_of_timestamp = normalize_timestamp(as_of.isoformat())
    filtered: list[dict[str, Any]] = []
    for item in records:
        occurred_at = str(item.get("occurred_at") or "")
        if occurred_at and occurred_at > as_of_timestamp:
            continue
        if from_at and occurred_at < from_at:
            continue
        if to_at and occurred_at > to_at:
            continue
        if direction and item.get("direction") != direction:
            continue
        if thread_key and str(item.get("thread_key") or "").casefold() != thread_key:
            continue
        if query:
            searchable = " ".join((
                str(item.get("title") or ""),
                str(item.get("summary") or ""),
                json.dumps(item.get("metadata") or {}, ensure_ascii=False),
            )).casefold()
            if not _query_matches(searchable, query):
                continue
        filtered.append(item)

    total = len(filtered)
    limit = max(1, min(int(args.get("limit") or 50), 100))
    try:
        page, next_cursor = paginate_records(
            filtered,
            key=lambda item: (
                str(item.get("occurred_at") or ""),
                f"{item.get('kind')}:{item.get('record_id')}",
            ),
            cursor=str(args.get("cursor") or "") or None,
            limit=limit,
            descending=True,
        )
    except ValueError as exc:
        return {"error": str(exc)}

    for item in page:
        locator = f"{item['kind']}:{item['record_id']}"
        item["source_id"] = add_source(
            args,
            kind=str(item["kind"]),
            title=str(item["title"]),
            locator=locator,
            excerpt=str(item.get("summary") or "")[:500],
            project_id=project_id,
            occurred_at=str(item.get("occurred_at") or ""),
            url=f"/projects/{quote(project_id)}",
            metadata=dict(item.get("metadata") or {}),
        )

    summary_source_id = add_source(
        args,
        kind="timeline_query",
        title=f"{project_id} timeline query",
        locator=(
            f"timeline-query:{project_id}:{','.join(sorted(kinds))}:{from_at or '*'}:"
            f"{to_at or '*'}:{query}:{direction}:{thread_key}:{as_of.isoformat()}"
        ),
        project_id=project_id,
        occurred_at=as_of.isoformat(),
        excerpt=f"Found {total} matching timeline records; returned {len(page)} on this page.",
        metadata={"total": total, "result_count": len(page), "kinds": sorted(kinds)},
    )
    return {
        "project_id": project_id,
        "as_of": as_of.isoformat(),
        "filters": {
            "kinds": sorted(kinds),
            "from": from_at,
            "to": to_at,
            "query": query or None,
            "direction": direction or None,
            "thread_id": thread_key or None,
        },
        "total": total,
        "result_count": len(page),
        "next_cursor": next_cursor,
        "summary_source_id": summary_source_id,
        "results": page,
        "citation_instruction": "Cite timeline facts with each record's source_id.",
    }


def list_unanswered_threads(
    conn: sqlite3.Connection,
    args: dict[str, Any],
) -> dict[str, Any]:
    """List inbound-last conversation candidates across the trusted scope."""
    explicit_project_id = str(args.get("project_id") or "").strip()
    if explicit_project_id:
        ensure_project_in_scope(args, explicit_project_id)
        if not _project_exists(conn, explicit_project_id):
            return {"error": f"Project {explicit_project_id} not found"}
        project_ids = [explicit_project_id]
    else:
        include_terminal = bool(args.get("include_terminal", False)) or bool(
            args.get("_assistant_all_projects_requested")
        )
        project_ids = visible_project_ids(
            conn, args, include_terminal=include_terminal
        )

    as_of = parse_as_of(args.get("_assistant_as_of"))
    raw_min_age = args.get("min_age_hours")
    min_age_hours = max(
        0.0,
        float(24.0 if raw_min_age is None else raw_min_age),
    )
    records: list[dict[str, Any]] = []
    for project_id in project_ids:
        project = conn.execute(
            "SELECT client_name, project_status FROM projects WHERE project_id=?",
            (project_id,),
        ).fetchone()
        for thread in resolve_project_email_threads(conn, project_id):
            if not thread.candidate_unanswered:
                continue
            latest = thread.latest
            occurred_at = str(latest.get("occurred_at") or "")
            try:
                occurred = datetime.fromisoformat(occurred_at)
                age_hours = max(0.0, (as_of - occurred).total_seconds() / 3600)
            except (TypeError, ValueError):
                age_hours = 0.0
            if age_hours < min_age_hours:
                continue
            insight = conn.execute(
                """SELECT action_required, suggested_action, confidence, category,
                          suppressed, suppression_reason
                   FROM inbox_insights WHERE email_id=?
                   ORDER BY updated_at DESC LIMIT 1""",
                (latest["email_id"],),
            ).fetchone()
            records.append({
                "project_id": project_id,
                "client_name": project["client_name"] if project else None,
                "project_status": project["project_status"] if project else None,
                "thread_key": thread.thread_key,
                "latest_email_id": latest["email_id"],
                "subject": latest.get("subject"),
                "sender_email": latest.get("sender_email"),
                "occurred_at": occurred_at,
                "age_hours": round(age_hours, 1),
                "message_count": len(thread.messages),
                "body_snippet": str(latest.get("body_snippet") or "")[:800],
                "candidate_state": "inbound_last_no_newer_outbound",
                "actionability": dict(insight) if insight else None,
            })

    total = len(records)
    limit = max(1, min(int(args.get("limit") or 50), 100))
    try:
        page, next_cursor = paginate_records(
            records,
            key=lambda item: (
                str(item.get("occurred_at") or ""),
                f"{item.get('project_id')}:{item.get('latest_email_id')}",
            ),
            cursor=str(args.get("cursor") or "") or None,
            limit=limit,
            descending=False,
        )
    except ValueError as exc:
        return {"error": str(exc)}

    for item in page:
        item["source_id"] = add_source(
            args,
            kind="email",
            title=str(item.get("subject") or "Inbound email thread"),
            locator=f"processed_email:{item['latest_email_id']}",
            excerpt=str(item.get("body_snippet") or "")[:500],
            project_id=str(item["project_id"]),
            occurred_at=str(item.get("occurred_at") or ""),
            url=f"/projects/{quote(str(item['project_id']))}",
            metadata={
                "thread_key": item["thread_key"],
                "candidate_state": item["candidate_state"],
                "direction": "inbound",
            },
        )

    summary_source_id = add_source(
        args,
        kind="unanswered_thread_query",
        title="Unanswered thread query",
        locator=(
            f"unanswered-thread-query:{','.join(project_ids)}:{explicit_project_id or '*'}:"
            f"{min_age_hours}:{as_of.isoformat()}"
        ),
        occurred_at=as_of.isoformat(),
        excerpt=(
            f"Scanned {len(project_ids)} projects and found {total} inbound-last threads "
            f"with no newer outbound response as of {as_of.isoformat()}."
        ),
        metadata={
            "project_ids_scanned": project_ids,
            "min_age_hours": min_age_hours,
            "total": total,
            "result_count": len(page),
        },
    )
    return {
        "as_of": as_of.isoformat(),
        "summary_source_id": summary_source_id,
        "project_ids_scanned": project_ids,
        "candidate_definition": (
            "The latest message in the resolved thread is inbound and no newer outbound "
            "message exists. This is a response candidate, not proof that a reply is required."
        ),
        "filters": {
            "project_id": explicit_project_id or None,
            "min_age_hours": min_age_hours,
        },
        "total": total,
        "result_count": len(page),
        "next_cursor": next_cursor,
        "results": page,
        "citation_instruction": "Cite each candidate with its source_id.",
    }
