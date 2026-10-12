"""Read-only domain tools for the AI Workspace agent profile."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from datetime import datetime, timezone
from typing import Any
from urllib.parse import quote

from src.assistant.pm_project_intelligence import (
    assess_milestone_readiness,
    get_project_evidence_audit,
    list_project_work,
)
from src.assistant.pm_timeline import (
    list_unanswered_threads,
    search_project_timeline,
)
from src.assistant.portfolio_query_v2 import PortfolioQueryV2Request, portfolio_query_v2
from src.assistant.web_search import get_web_search_provider
from src.assistant.workspace_read_support import decode_cursor, encode_cursor, project_row_as_of
from src.portfolio.portfolio_scan import scan_portfolio

_TERMINAL_STATUSES = ("Completed", "Archived", "Cancelled")


def _scope(args: dict[str, Any]) -> tuple[str, list[str]]:
    mode = str(args.get("_assistant_scope_mode") or "portfolio")
    raw_ids = args.get("_assistant_scope_project_ids") or []
    project_ids = list(dict.fromkeys(str(item) for item in raw_ids if str(item).strip()))
    return mode, project_ids


def _ensure_project_in_scope(args: dict[str, Any], project_id: str) -> None:
    mode, project_ids = _scope(args)
    if mode == "projects" and project_id not in project_ids:
        raise PermissionError(f"Project {project_id} is outside the active Workspace scope")


def _source_id(kind: str, locator: str) -> str:
    digest = hashlib.sha1(f"{kind}:{locator}".encode("utf-8")).hexdigest()[:12]
    return f"{kind}-{digest}"


def _add_source(
    args: dict[str, Any],
    *,
    kind: str,
    title: str,
    locator: str,
    excerpt: str = "",
    project_id: str | None = None,
    occurred_at: str | None = None,
    url: str | None = None,
    metadata: dict[str, Any] | None = None,
) -> str:
    source_id = _source_id(kind, locator)
    sink = args.get("_assistant_source_sink")
    record = {
        "type": "source",
        "source_id": source_id,
        "kind": kind,
        "title": title,
        "locator": locator,
        "snippet": excerpt,
        "project_id": project_id,
        "occurred_at": occurred_at,
        "url": url,
        "metadata": metadata or {},
    }
    if isinstance(sink, list) and not any(item.get("source_id") == source_id for item in sink):
        sink.append(record)
    return source_id


def _parse_as_of(value: Any) -> datetime:
    if isinstance(value, datetime):
        current = value
    else:
        raw = str(value or "").strip()
        try:
            current = datetime.fromisoformat(raw.replace("Z", "+00:00")) if raw else datetime.now(timezone.utc)
        except ValueError:
            current = datetime.now(timezone.utc)
    return current if current.tzinfo else current.replace(tzinfo=timezone.utc)


def workspace_portfolio_attention(conn: sqlite3.Connection, args: dict[str, Any]) -> dict[str, Any]:
    """Return deterministic portfolio attention ranking with source records."""
    as_of = _parse_as_of(args.get("_assistant_as_of"))
    summary = scan_portfolio(conn, now=as_of)
    mode, project_ids = _scope(args)
    allowed = set(project_ids) if mode == "projects" else None
    limit = max(1, min(int(args.get("limit") or 50), 100))
    include_on_track = bool(args.get("include_on_track", False))
    rows: list[dict[str, Any]] = []
    for item in summary.items:
        if allowed is not None and item.project_id not in allowed:
            continue
        if not include_on_track and item.priority_score <= 0:
            continue
        locator = f"portfolio-attention:{item.project_id}:{as_of.date().isoformat()}"
        source_id = _add_source(
            args,
            kind="project",
            title=f"{item.project_id} attention snapshot",
            locator=locator,
            project_id=item.project_id,
            occurred_at=summary.generated_at,
            url=f"/projects/{quote(item.project_id)}",
            excerpt=(
                f"Status {item.project_status}; milestone {item.current_milestone or 'not set'}; "
                f"ball with {item.ball_with or 'Unknown'}; priority {int(item.priority_score)}; "
                f"signals: {', '.join(item.flags)}."
            ),
            metadata={"as_of": as_of.isoformat(), "flags": item.flags},
        )
        rows.append({
            "project_id": item.project_id,
            "client_name": item.client_name,
            "project_status": item.project_status,
            "current_milestone": item.current_milestone,
            "ball_with": item.ball_with,
            "priority_score": int(item.priority_score),
            "days_inactive": item.days_inactive,
            "attention_reasons": item.flags,
            "next_action": item.next_action,
            "source_id": source_id,
        })
        if len(rows) >= limit:
            break
    scanned = len([item for item in summary.items if allowed is None or item.project_id in allowed])
    summary_source_id = _add_source(
        args,
        kind="portfolio",
        title="Portfolio attention summary",
        locator=(
            f"portfolio-attention-summary:{mode}:{','.join(project_ids)}:"
            f"{as_of.date().isoformat()}:{include_on_track}"
        ),
        occurred_at=summary.generated_at,
        excerpt=(
            f"Scanned {scanned} active projects and found {len(rows)} matching the attention criteria "
            f"as of {as_of.isoformat()}."
        ),
        metadata={
            "scope_mode": mode,
            "project_ids": project_ids,
            "projects_scanned": scanned,
            "attention_count": len(rows),
        },
    )
    return {
        "as_of": as_of.isoformat(),
        "projects_scanned": scanned,
        "attention_count": len(rows),
        "summary_source_id": summary_source_id,
        "results": rows,
        "citation_instruction": "Cite each project fact with its source_id using [[source:<source_id>]].",
    }


def workspace_search_project_parties(conn: sqlite3.Connection, args: dict[str, Any]) -> dict[str, Any]:
    """Search the canonical project-party graph, including completed projects by default."""
    party_name = str(args.get("party_name") or "").strip()
    if not party_name:
        return {"error": "party_name is required"}
    party_role = str(args.get("party_role") or "").strip().lower()
    include_terminal = bool(args.get("include_terminal", True))
    mode, project_ids = _scope(args)
    clauses = ["LOWER(o.org_name) LIKE LOWER(?)", "p.project_id != '_system'"]
    params: list[Any] = [f"%{party_name}%"]
    if party_role:
        clauses.append("pp.party_role = ?")
        params.append(party_role)
    if not include_terminal:
        clauses.append("p.project_status NOT IN (?, ?, ?)")
        params.extend(_TERMINAL_STATUSES)
    if mode == "projects":
        if not project_ids:
            clauses.append("1 = 0")
        else:
            placeholders = ",".join("?" for _ in project_ids)
            clauses.append(f"p.project_id IN ({placeholders})")
            params.extend(project_ids)
    rows = conn.execute(
        f"""SELECT pp.id, pp.project_id, pp.party_role, pp.is_primary, pp.notes,
                   o.org_id, o.org_name, o.org_type, p.client_name, p.project_status,
                   p.current_milestone, p.ball_with, p.next_action
            FROM project_parties pp
            JOIN organisations o ON o.org_id = pp.org_id
            JOIN projects p ON p.project_id = pp.project_id
            WHERE {' AND '.join(clauses)}
            ORDER BY o.org_name, p.project_id""",
        params,
    ).fetchall()
    results: list[dict[str, Any]] = []
    for row in rows:
        item = dict(row)
        source_id = _add_source(
            args,
            kind="party",
            title=f"{item['org_name']} on {item['project_id']}",
            locator=f"project-party:{item['id']}",
            project_id=item["project_id"],
            url=f"/projects/{quote(item['project_id'])}",
            excerpt=(
                f"{item['org_name']} is linked to {item['project_id']} as {item['party_role']}; "
                f"project status {item['project_status']} and milestone {item['current_milestone'] or 'not set'}."
            ),
            metadata={"org_id": item["org_id"], "party_role": item["party_role"]},
        )
        item["source_id"] = source_id
        results.append(item)
    as_of = _parse_as_of(args.get("_assistant_as_of"))
    summary_source_id = _add_source(
        args,
        kind="portfolio_query",
        title=f"Organisation search: {party_name}",
        locator=(
            f"project-party-search:{party_name.lower()}:{party_role or '*'}:"
            f"{include_terminal}:{mode}:{','.join(project_ids)}:{as_of.date().isoformat()}"
        ),
        occurred_at=as_of.isoformat(),
        excerpt=(
            f"Found {len(results)} project-party relationships matching {party_name!r}; "
            f"terminal projects {'included' if include_terminal else 'excluded'}."
        ),
        metadata={
            "party_name": party_name,
            "party_role": party_role or None,
            "include_terminal": include_terminal,
            "total": len(results),
        },
    )
    return {
        "query": party_name,
        "as_of": as_of.isoformat(),
        "include_terminal": include_terminal,
        "total": len(results),
        "summary_source_id": summary_source_id,
        "results": results,
        "citation_instruction": "Cite each relationship with its source_id using [[source:<source_id>]].",
    }


def workspace_query_portfolio(conn: sqlite3.Connection, args: dict[str, Any]) -> dict[str, Any]:
    """Query projects through structured, allowlisted filters and trusted scope."""
    status_transition = args.get("status_transition")
    if status_transition is not None:
        return _workspace_query_portfolio_transition(conn, args, status_transition)
    mode, project_ids = _scope(args)
    filters = {
        key: args.get(key)
        for key in ("project_status", "current_milestone", "ball_with", "market", "client_name")
        if args.get(key)
    }
    search_text = str(args.get("search_text") or "").strip() or None
    requested_include_terminal = bool(args.get("include_terminal", False))
    trusted_all_projects = bool(args.get("_assistant_all_projects_requested"))
    effective_include_terminal = requested_include_terminal or trusted_all_projects
    limit = 100 if trusted_all_projects else max(1, min(int(args.get("limit") or 50), 100))

    clauses = ["project_id != ?"]
    params: list[Any] = ["_system"]
    for field_name, value in filters.items():
        clauses.append(f"{field_name} = ?")
        params.append(value)
    if search_text:
        clauses.append("(client_name LIKE ? OR project_id LIKE ? OR notes LIKE ?)")
        like_value = f"%{search_text}%"
        params.extend([like_value, like_value, like_value])
    if not effective_include_terminal and "project_status" not in filters:
        clauses.append("project_status NOT IN (?, ?, ?)")
        params.extend(_TERMINAL_STATUSES)
    if mode == "projects":
        if not project_ids:
            clauses.append("1 = 0")
        else:
            placeholders = ",".join("?" for _ in project_ids)
            clauses.append(f"project_id IN ({placeholders})")
            params.extend(project_ids)

    where = " AND ".join(clauses)
    total = int(conn.execute(
        f"SELECT COUNT(*) FROM projects WHERE {where}", params
    ).fetchone()[0])

    try:
        cursor_key = decode_cursor(str(args.get("cursor") or "") or None)
    except ValueError as exc:
        return {"error": str(exc)}
    if cursor_key is not None and len(cursor_key) != 1:
        return {"error": "Invalid cursor"}

    page_clauses = list(clauses)
    page_params = list(params)
    if cursor_key is not None:
        page_clauses.append("project_id > ?")
        page_params.append(cursor_key[0])
    page_params.append(limit + 1)
    raw_rows = conn.execute(
        f"SELECT * FROM projects WHERE {' AND '.join(page_clauses)} "
        "ORDER BY project_id ASC LIMIT ?",
        page_params,
    ).fetchall()
    has_more = len(raw_rows) > limit
    raw_rows = raw_rows[:limit]

    rows: list[dict[str, Any]] = []
    for raw in raw_rows:
        item = dict(raw)
        source_id = _add_source(
            args,
            kind="project",
            title=f"{item['project_id']} project record",
            locator=f"project:{item['project_id']}:{item.get('updated_at') or ''}",
            project_id=item["project_id"],
            occurred_at=item.get("updated_at"),
            url=f"/projects/{quote(item['project_id'])}",
            excerpt=(
                f"{item['project_id']} is {item.get('project_status')}; milestone "
                f"{item.get('current_milestone') or 'not set'}; ball with {item.get('ball_with')}."
            ),
        )
        item["source_id"] = source_id
        rows.append(item)
    next_cursor = encode_cursor((rows[-1]["project_id"],)) if has_more and rows else None

    as_of = _parse_as_of(args.get("_assistant_as_of"))
    summary_source_id = _add_source(
        args,
        kind="portfolio_query",
        title="Portfolio query summary",
        locator=(
            "portfolio-query:"
            + hashlib.sha1(json.dumps({
                "filters": filters,
                "search_text": search_text,
                "requested_include_terminal": requested_include_terminal,
                "effective_include_terminal": effective_include_terminal,
                "scope_mode": mode,
                "project_ids": project_ids,
                "cursor": cursor_key,
                "as_of": as_of.date().isoformat(),
            }, sort_keys=True, default=str).encode("utf-8")).hexdigest()[:16]
        ),
        occurred_at=as_of.isoformat(),
        excerpt=(
            f"The portfolio query matched {total} projects in the active Workspace scope "
            f"and returned {len(rows)} on this page."
        ),
        metadata={
            "filters": filters,
            "search_text": search_text,
            "requested_include_terminal": requested_include_terminal,
            "effective_include_terminal": effective_include_terminal,
            "trusted_all_projects_requested": trusted_all_projects,
            "total": total,
            "result_count": len(rows),
        },
    )
    return {
        "as_of": as_of.isoformat(),
        "filters": filters,
        "requested_include_terminal": requested_include_terminal,
        "effective_include_terminal": effective_include_terminal,
        "trusted_all_projects_requested": trusted_all_projects,
        "total": total,
        "result_count": len(rows),
        "next_cursor": next_cursor,
        "summary_source_id": summary_source_id,
        "results": rows,
        "citation_instruction": "Cite project facts with their source_id using [[source:<source_id>]].",
    }


def _workspace_query_portfolio_transition(
    conn: sqlite3.Connection,
    args: dict[str, Any],
    status_transition: Any,
) -> dict[str, Any]:
    """Serve historical status reads from accepted operator audit records only."""
    if not isinstance(status_transition, dict):
        return {"error": "status_transition must be an object"}
    requested_status = str(status_transition.get("to_status") or "").strip()
    effective_from = str(status_transition.get("from") or "").strip() or None
    effective_to = str(status_transition.get("to") or "").strip() or None
    try:
        request = PortfolioQueryV2Request.model_validate(
            {
                "operation": "list",
                "filters": {
                    "transition": {
                        "field": "project_status",
                        "to_values": [requested_status],
                        "effective_from": effective_from,
                        "effective_to": effective_to,
                    }
                },
                "include_terminal": False,
                "group_by": None,
                "fields": [
                    "project_id",
                    "client_name",
                    "project_status",
                    "transitioned_at",
                ],
                "sort": [{"field": "transitioned_at", "direction": "desc"}],
                "limit": max(1, min(int(args.get("limit") or 50), 100)),
                "cursor": str(args.get("cursor") or "").strip() or None,
            }
        )
    except (TypeError, ValueError) as exc:
        return {"error": str(exc)}

    result = portfolio_query_v2(
        conn,
        {
            **request.model_dump(mode="json"),
            "_assistant_scope_mode": _scope(args)[0],
            "_assistant_scope_project_ids": _scope(args)[1],
            "_assistant_source_sink": args.get("_assistant_source_sink"),
            "_assistant_as_of": _parse_as_of(args.get("_assistant_as_of")).isoformat(),
        },
    )
    return {
        **result,
        "filters": {"status_transition": dict(status_transition)},
        "requested_include_terminal": False,
        "effective_include_terminal": False,
        "trusted_all_projects_requested": bool(args.get("_assistant_all_projects_requested")),
        "result_count": len(result.get("results") or []),
        "citation_instruction": (
            "Cite transition facts with their audit-backed source_id using "
            "[[source:<source_id>]]."
        ),
    }


def workspace_get_project_brief(conn: sqlite3.Connection, args: dict[str, Any]) -> dict[str, Any]:
    """Return a source-linked project snapshot plus recent evidence as of the requested time."""
    project_id = str(args.get("project_id") or "").strip()
    if not project_id:
        return {"error": "project_id is required"}
    _ensure_project_in_scope(args, project_id)
    as_of = _parse_as_of(args.get("_assistant_as_of"))
    as_of_utc = as_of.astimezone(timezone.utc).isoformat()
    project = project_row_as_of(conn, project_id, as_of)
    if project is None:
        return {"error": f"Project {project_id} not found"}

    project_source = _add_source(
        args,
        kind="project",
        title=f"{project_id} project record as of {as_of.isoformat()}",
        locator=f"project:{project_id}:{project.get('updated_at') or as_of.isoformat()}:as-of:{as_of.isoformat()}",
        project_id=project_id,
        occurred_at=project.get("updated_at") or as_of.isoformat(),
        url=f"/projects/{quote(project_id)}",
        excerpt=(
            f"As of {as_of.isoformat()}: status {project.get('project_status')}; "
            f"milestone {project.get('current_milestone')}; ball with {project.get('ball_with')}; "
            f"target {project.get('target_go_live') or 'not set'}; "
            f"next action: {project.get('next_action') or 'not set'}."
        ),
        metadata={
            "as_of": as_of.isoformat(),
            "snapshot_reconstructed": bool(project.get("snapshot_reconstructed")),
            "snapshot_rewound_fields": project.get("snapshot_rewound_fields") or [],
        },
    )
    project["source_id"] = project_source

    party_rows = conn.execute(
        """SELECT pp.id, pp.party_role, pp.is_primary, pp.notes, o.org_id, o.org_name, o.org_type
           FROM project_parties pp JOIN organisations o ON o.org_id=pp.org_id
           WHERE pp.project_id=? ORDER BY pp.party_role, o.org_name""",
        (project_id,),
    ).fetchall()
    parties: list[dict[str, Any]] = []
    for raw in party_rows:
        item = dict(raw)
        item["source_id"] = _add_source(
            args,
            kind="party",
            title=f"{item['org_name']} on {project_id}",
            locator=f"project-party:{item['id']}",
            project_id=project_id,
            url=f"/projects/{quote(project_id)}",
            excerpt=f"{item['org_name']} is linked as {item['party_role']} on {project_id}.",
            metadata={"org_id": item["org_id"], "party_role": item["party_role"]},
        )
        parties.append(item)

    milestone_rows = conn.execute(
        """SELECT id, from_milestone, to_milestone, changed_by, changed_at, reason, evidence_ref
           FROM milestone_history
           WHERE project_id=? AND datetime(changed_at) <= datetime(?)
           ORDER BY datetime(changed_at) DESC LIMIT 10""",
        (project_id, as_of_utc),
    ).fetchall()
    milestones: list[dict[str, Any]] = []
    for raw in milestone_rows:
        item = dict(raw)
        item["source_id"] = _add_source(
            args,
            kind="milestone",
            title=f"{project_id} milestone change",
            locator=f"milestone-history:{item['id']}",
            project_id=project_id,
            occurred_at=item.get("changed_at"),
            url=f"/projects/{quote(project_id)}",
            excerpt=f"Milestone changed from {item.get('from_milestone')} to {item.get('to_milestone')}: {item.get('reason') or 'no reason recorded'}.",
        )
        milestones.append(item)

    activity_rows = conn.execute(
        """SELECT id, actor, surface, action_type, object_ref, summary, metadata_json, created_at
           FROM activity_log
           WHERE project_id=? AND datetime(created_at) <= datetime(?)
           ORDER BY datetime(created_at) DESC, id DESC LIMIT 12""",
        (project_id, as_of_utc),
    ).fetchall()
    activities: list[dict[str, Any]] = []
    for raw in activity_rows:
        item = dict(raw)
        item["source_id"] = _add_source(
            args,
            kind="activity",
            title=f"{project_id} activity: {item.get('action_type') or 'update'}",
            locator=f"activity:{item['id']}",
            project_id=project_id,
            occurred_at=item.get("created_at"),
            url=f"/projects/{quote(project_id)}",
            excerpt=str(item.get("summary") or "")[:500],
            metadata={"actor": item.get("actor"), "surface": item.get("surface")},
        )
        activities.append(item)

    emails: list[dict[str, Any]] = []
    processed = conn.execute(
        """SELECT email_id AS id, 'processed_email' AS record_type, received_at, processed_at,
                  subject, sender_email, direction, body_snippet AS body
           FROM processed_emails
           WHERE project_id=?
             AND (COALESCE(received_at, processed_at) IS NULL
                  OR datetime(COALESCE(received_at, processed_at)) <= datetime(?))
           ORDER BY datetime(COALESCE(received_at, processed_at)) DESC LIMIT 10""",
        (project_id, as_of_utc),
    ).fetchall()
    inbound = conn.execute(
        """SELECT reply_id AS id, 'inbound_reply' AS record_type, received_at, created_at AS processed_at,
                  subject, NULL AS sender_email, 'inbound' AS direction, body_text AS body
           FROM inbound_replies
           WHERE project_id=? AND (received_at IS NULL OR datetime(received_at) <= datetime(?))
           ORDER BY datetime(received_at) DESC LIMIT 10""",
        (project_id, as_of_utc),
    ).fetchall()
    for raw in [*processed, *inbound]:
        item = dict(raw)
        occurred_at = item.get("received_at") or item.get("processed_at") or ""
        item["occurred_at"] = occurred_at
        item["body"] = str(item.get("body") or "")[:1200]
        item["source_id"] = _add_source(
            args,
            kind="email",
            title=str(item.get("subject") or f"{project_id} email"),
            locator=f"{item['record_type']}:{item['id']}",
            project_id=project_id,
            occurred_at=occurred_at,
            url=f"/projects/{quote(project_id)}",
            excerpt=item["body"][:500],
            metadata={"direction": item.get("direction"), "sender_email": item.get("sender_email")},
        )
        emails.append(item)
    emails.sort(key=lambda item: str(item.get("occurred_at") or ""), reverse=True)
    emails = emails[:12]

    as_of = _parse_as_of(args.get("_assistant_as_of"))
    summary_source_id = _add_source(
        args,
        kind="project_snapshot",
        title=f"{project_id} Workspace snapshot",
        locator=f"project-snapshot:{project_id}:{as_of.isoformat()}",
        project_id=project_id,
        occurred_at=as_of.isoformat(),
        url=f"/projects/{quote(project_id)}",
        excerpt=f"Workspace project snapshot captured as of {as_of.isoformat()}.",
        metadata={"as_of": as_of.isoformat()},
    )
    return {
        "as_of": as_of.isoformat(),
        "summary_source_id": summary_source_id,
        "project": project,
        "parties": parties,
        "milestone_history": milestones,
        "recent_activity": activities,
        "recent_emails": emails,
        "evidence_guidance": (
            "The project row is authoritative for current state. Activities and emails are historical evidence; "
            "do not treat a contact-only or planning message as completion evidence unless it explicitly proves the transition. "
            "Keep recommendations distinct from completed changes."
        ),
        "citation_instruction": "Cite each factual claim with the relevant source_id using [[source:<source_id>]].",
    }


def workspace_web_search(conn: sqlite3.Connection, args: dict[str, Any]) -> dict[str, Any]:
    """Run configured external search only when the trusted thread snapshot enables it."""
    if not bool(args.get("_assistant_web_enabled")):
        return {"error": "Web Search is disabled for this Workspace turn"}
    query = str(args.get("query") or "").strip()
    if not query:
        return {"error": "query is required"}
    text, sources = get_web_search_provider().search(query, limit=max(1, min(int(args.get("limit") or 5), 8)))
    normalized: list[dict[str, Any]] = []
    for source in sources:
        source_id = _add_source(
            args,
            kind="web",
            title=source.title,
            locator=source.url,
            url=source.url,
            excerpt=source.snippet,
        )
        normalized.append({
            "source_id": source_id,
            "title": source.title,
            "url": source.url,
            "snippet": source.snippet,
        })
    if not normalized:
        return {"error": "Web Search returned no verifiable source records"}
    return {
        "query": query,
        "summary": text,
        "sources": normalized,
        "citation_instruction": "Cite web facts with source_id using [[source:<source_id>]].",
    }


ASSISTANT_WORKSPACE_TOOL_HANDLERS = {
    "portfolio_attention": workspace_portfolio_attention,
    "search_project_parties": workspace_search_project_parties,
    "query_portfolio": workspace_query_portfolio,
    "get_project_brief": workspace_get_project_brief,
    "search_project_timeline": search_project_timeline,
    "list_project_work": list_project_work,
    "list_unanswered_threads": list_unanswered_threads,
    "get_project_evidence_audit": get_project_evidence_audit,
    "assess_milestone_readiness": assess_milestone_readiness,
    "web_search": workspace_web_search,
}
