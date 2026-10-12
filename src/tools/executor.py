"""Unified tool execution — single flat dispatch dict."""

import hashlib
import json
from contextlib import contextmanager
from contextvars import copy_context
import logging
from pathlib import Path
import sqlite3
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Callable

from opentelemetry import trace
from opentelemetry.trace import Status, StatusCode

from src.agent.tool_receipts import ToolExecutionReceipt, receipt_from_result
from src.db.connection import scoped_write

from pydantic import BaseModel

from config.settings import BASE_DIR

_WIKI_DIR = BASE_DIR / "wiki"
from src.observability.progress import ProgressCallback, emit_progress
from src.observability.contracts import current_trace_context, new_identifier
from src.observability.lifecycle import emit_event
from src.repositories import evidence_repo, milestone_history_repo, preference_repo
from src.repositories import contact_repo, project_repo, response_pattern_repo, working_memory_repo
from src.email.draft_session import create_draft
from src.email.draft_grounding import (
    attach_docs_to_draft as _attach_docs_to_draft,
    resolve_draft_doc_keys as _resolve_draft_doc_keys,
)
from src.tools.artifact_handlers import ARTIFACT_TOOL_HANDLERS
from src.tools.assistant_workspace_handlers import ASSISTANT_WORKSPACE_TOOL_HANDLERS
from src.tools.calendar_tools import CALENDAR_TOOL_HANDLERS
from src.tools.chat_handlers import CHAT_TOOL_HANDLERS
from src.tools.communication_tools import COMMUNICATION_TOOL_HANDLERS
from src.tools.evidence_handlers import EVIDENCE_TOOL_HANDLERS
from src.tools.internal_action_handlers import INTERNAL_ACTION_TOOL_HANDLERS
from src.tools.fix_session_handlers import FIX_SESSION_TOOL_HANDLERS
from src.tools.knowledge_handlers import KNOWLEDGE_TOOL_HANDLERS
from src.tools.learning_handlers import LEARNING_TOOL_HANDLERS
from src.tools.personal_work_tools import PERSONAL_WORK_TOOL_HANDLERS
from src.tools.project_intake_handlers import PROJECT_INTAKE_TOOL_HANDLERS
from src.tools.project_reconciliation_handlers import PROJECT_RECONCILIATION_TOOL_HANDLERS
from src.tools.support_chat_handlers import SUPPORT_CHAT_TOOL_HANDLERS
from src.tools.work_item_tools import WORK_ITEM_CHAT_TOOL_HANDLERS
from src.telegram.review_push import push_review_card_sync
from src.tools.context_confidence import ContextConfidenceError
from src.tools.execution_gateway import (
    ToolContractValidationError,
    ToolExecutionGateway,
)
from src.tools.health_tracker import get_tracker as get_tool_health_tracker
from src.tools.registry import TOOL_BY_NAME

logger = logging.getLogger(__name__)


def get_active_write_connection_factory():
    """Return the trusted writer factory bound to the current tool invocation."""
    context = getattr(_execution_local, "execution_context", None) or {}
    return context.get("write_connection_factory")


def get_active_chat_id() -> int | None:
    """Return the Telegram chat ID from the current tool execution context."""
    value = getattr(_execution_local, "chat_id", None)
    return value if isinstance(value, int) else None


_execution_local = threading.local()


@contextmanager
def trusted_execution_context(**values: Any):
    """Temporarily bind trusted runtime provenance outside model arguments."""
    previous = getattr(_execution_local, "execution_context", None)
    merged = dict(previous or {})
    merged.update({key: value for key, value in values.items() if value is not None})
    _execution_local.execution_context = merged
    try:
        yield
    finally:
        _execution_local.execution_context = previous


# ── Handler implementations ──────────────────────────────────────

def _handle_get_project_truth(conn: sqlite3.Connection, arguments: dict[str, Any]) -> dict[str, Any]:
    project_id = arguments["project_id"]
    project = project_repo.get_project(conn, project_id)
    milestone_history = milestone_history_repo.get_milestone_history(conn, project_id)
    return {
        "project": _normalize_for_json(project.model_dump()) if project else None,
        "milestone_history": _normalize_for_json(milestone_history),
    }


def _normalize_message_id(value: Any) -> str:
    raw = str(value or "").strip()
    if not raw:
        return ""
    return raw[1:-1].strip() if raw.startswith("<") and raw.endswith(">") else raw


def _escaped_subject_tag(project_id: str) -> str:
    escape = chr(92)
    escaped = (project_id.replace(escape, escape * 2)
               .replace("%", escape + "%").replace("_", escape + "_"))
    return f"%[{escaped}]%"


def _email_scope_sql(project_id: str) -> tuple[str, list[str]]:
    escape = chr(92)
    sql = "(project_id = ? OR (project_id IS NULL AND subject LIKE ? ESCAPE '" + escape + "'))"
    return sql, [project_id, _escaped_subject_tag(project_id)]


def _processed_email_fields() -> str:
    return ("email_id, project_id, processed_at, result, subject, sender_email, direction, "
            "cc, thread_id, in_reply_to, references_chain, body_snippet")


def _with_project_match(row: sqlite3.Row, project_id: str | None) -> dict[str, Any]:
    item = dict(row)
    if project_id:
        item["project_match"] = ("project_id" if item.get("project_id") == project_id
                                 else "subject_tag")
    if "body_snippet" in item:
        item["body_preview"] = item.get("body_snippet") or ""
    return item


def _find_processed_email(conn: sqlite3.Connection, email_id: str) -> sqlite3.Row | None:
    canonical = _normalize_message_id(email_id)
    rows = conn.execute(
        f"SELECT {_processed_email_fields()} FROM processed_emails "
        "WHERE email_id IN (?, ?, ?)",
        (email_id, canonical, f"<{canonical}>"),
    ).fetchall()
    return rows[0] if rows else None


def _handle_search_emails(conn: sqlite3.Connection, arguments: dict[str, Any]) -> dict[str, Any]:
    clauses: list[str] = []
    params: list[Any] = []
    project_id = arguments.get("project_id")
    if project_id:
        scope_sql, scope_params = _email_scope_sql(project_id)
        clauses.append(scope_sql)
        params.extend(scope_params)
    contact_email = arguments.get("contact_email")
    if contact_email:
        clauses.append("sender_email = ?")
        params.append(contact_email)
    keyword = arguments.get("keyword")
    if keyword:
        clauses.append("(subject LIKE ? OR result LIKE ? OR email_id LIKE ?)")
        pattern = f"%{keyword}%"
        params.extend([pattern, pattern, pattern])
    where_clause = " AND ".join(clauses) if clauses else "1=1"
    limit = int(arguments.get("limit", 10))
    total_count = conn.execute(
        f"SELECT COUNT(*) FROM processed_emails WHERE {where_clause}", params,
    ).fetchone()[0]
    rows = conn.execute(
        f"SELECT {_processed_email_fields()} FROM processed_emails "
        f"WHERE {where_clause} ORDER BY processed_at DESC LIMIT ?",
        [*params, limit],
    ).fetchall()
    return {"emails": [_with_project_match(row, project_id) for row in rows],
            "total_count": total_count}


def _handle_get_working_memory(conn: sqlite3.Connection, arguments: dict[str, Any]) -> dict[str, Any]:
    return {"memories": working_memory_repo.get_all_memory(conn, arguments["project_id"])}


def _handle_get_relationship_prefs(conn: sqlite3.Connection, arguments: dict[str, Any]) -> dict[str, Any]:
    contact_id = arguments["contact_id"]
    project_id = arguments.get("project_id")
    from src.context.governed_memory import compatibility_memory_request
    context = getattr(_execution_local, "execution_context", {}) or {}
    request = compatibility_memory_request(conn, operator_id=context.get("operator_id"),
        project_id=project_id, contact_id=contact_id, surface="tool", task_type="get_relationship_prefs")
    pattern = response_pattern_repo.get_response_pattern(conn, contact_id, project_id,
        memory_request=request, for_context=True)
    effective = preference_repo.get_effective_preference(conn, project_id) if project_id else None
    avoid = _parse_json_list(pattern.get("avoid_phrases")) if pattern else []
    signals = _parse_json_list(pattern.get("positive_signals")) if pattern else []
    result = {
        "preferred_greeting": pattern.get("preferred_greeting") if pattern else None,
        "tone": pattern.get("tone") if pattern else None,
        "avoid_phrases": avoid,
        "positive_signals": signals,
        "preferred_language": pattern.get("preferred_language") if pattern else None,
        "response_length_preference": pattern.get("response_length_preference") if pattern else None,
    }
    if effective:
        result["tone"] = result["tone"] or effective.tone
        result["preferred_language"] = result["preferred_language"] or effective.language
    return result


def _handle_draft_reply(
    conn: sqlite3.Connection,
    arguments: dict[str, Any],
) -> dict[str, Any]:
    """Compatibility wrapper for the focused draft_reply implementation."""
    from src.tools.draft_reply_handler import handle_draft_reply

    execution_context = dict(
        getattr(_execution_local, "execution_context", {}) or {}
    )
    return handle_draft_reply(
        conn,
        arguments,
        execution_context=execution_context,
        create_draft_fn=create_draft,
    )


def _handle_push_telegram_review(conn: sqlite3.Connection, arguments: dict[str, Any]) -> dict[str, Any]:
    project_id = arguments.get("project_id")
    review_card = arguments.get("review_card") or {}
    if not project_id:
        return {"telegram_message_id": None, "sent_at": None, "status": "failed", "error": "project_id is required"}
    summary = review_card.get("proposed_change_summary", "Review requested.")
    reason = review_card.get("reason")
    risk_level = review_card.get("risk_level")
    update_id = arguments.get("update_id") or f"review_{project_id}_{datetime.now(timezone.utc).strftime('%Y%m%d%H%M%S')}"
    result = push_review_card_sync(
        conn, project_id=project_id, update_id=update_id, summary=summary,
        reason=reason, risk_level=risk_level,
        evidence_refs=arguments.get("evidence_refs"),
        proposed_changes=arguments.get("proposed_changes"),
        chat_id=arguments.get("chat_id"),
    )
    telegram_message_id = result.get("telegram_message_id")
    sent = result.get("sent", False)
    return {
        "telegram_message_id": str(telegram_message_id) if telegram_message_id else None,
        "sent_at": result.get("sent_at"),
        "status": "sent" if sent else "failed",
        "error": result.get("error"),
    }


def _handle_log_evidence(conn: sqlite3.Connection, arguments: dict[str, Any]) -> dict[str, Any]:
    evidence_item = arguments.get("evidence_item") or {}
    evidence_type = evidence_item.get("type")
    content_summary = evidence_item.get("content_summary")
    source_ref = evidence_item.get("source_ref")
    timestamp = evidence_item.get("timestamp") or datetime.now(timezone.utc).isoformat()
    if not evidence_type or not content_summary:
        return {"evidence_id": None, "appended_at": None, "error": "evidence_item.type and evidence_item.content_summary are required"}
    evidence_id = evidence_repo.insert_evidence(
        conn=conn, project_id=arguments["project_id"],
        evidence_type=evidence_type, content_summary=content_summary,
        content_text=evidence_item.get("content_text"),
        source_ref=source_ref, evidence_at=timestamp,
    )
    conn.commit()
    return {"evidence_id": evidence_id, "appended_at": datetime.now(timezone.utc).isoformat()}


def _handle_find_internal_contact(conn: sqlite3.Connection, arguments: dict[str, Any]) -> dict[str, Any]:
    expertise_needed = arguments["expertise_needed"]
    limit = int(arguments.get("limit", 5))
    contacts = contact_repo.find_internal_contacts_by_expertise(conn, expertise_needed, limit)
    return {"contacts": contacts, "total_count": len(contacts)}


def _handle_list_reference_docs(conn: sqlite3.Connection, arguments: dict[str, Any]) -> dict[str, Any]:
    # conn unused: pure in-memory catalog lookup, no DB access needed
    from src.email.reference_library import list_reference_docs
    return {"docs": list_reference_docs()}


def _handle_attach_reference_doc(conn: sqlite3.Connection, arguments: dict[str, Any]) -> dict[str, Any]:
    from config.settings import CONFIG
    from src.db.connection import scoped_write
    from src.email.attachment_recovery import attachment_visibility_lock
    from src.email.attachments import AttachmentError, list_attachments
    from src.email.draft_workspace import WorkspaceLockedError
    from src.email.reference_library import get_reference_doc, resolve_doc_path
    from src.services.committed_attachment_service import (
        add_committed_draft_attachment, explicit_attachment_write_scope,
    )

    draft_id = (arguments.get("draft_id") or "").strip()
    doc_key = (arguments.get("doc_key") or "").strip()
    if not draft_id:
        return {"ok": False, "error": "draft_id is required"}
    doc = get_reference_doc(doc_key)
    if doc is None:
        return {
            "ok": False,
            "error": f"Unknown doc_key: '{doc_key}'. Call list_reference_docs to see available documents.",
        }
    doc_path = resolve_doc_path(doc_key)
    if doc_path is None or not doc_path.exists():
        return {"ok": False, "error": f"File not found for doc_key '{doc_key}'. Check data/reference_docs/."}

    # Do not nest a writer or commit an unrelated caller-owned transaction.
    if not _connection_is_query_only(conn) and conn.in_transaction:
        return {
            "ok": False,
            "draft_id": draft_id,
            "error": "Attachment requires an idle caller connection; commit or roll back its transaction first.",
            "side_effect_status": "not_started",
        }
    if _connection_is_query_only(conn):
        write_scope = get_active_write_connection_factory() or scoped_write
    else:
        write_scope = lambda: explicit_attachment_write_scope(conn)
    try:
        data = doc_path.read_bytes()
    except OSError as exc:
        return {"ok": False, "draft_id": draft_id, "error": str(exc)}

    try:
        # Keep content deduplication and the journal command under the same
        # process-local visibility guard to prevent a delete/check race.
        with attachment_visibility_lock(CONFIG.data_dir):
            if not conn.execute(
                "SELECT 1 FROM drafts WHERE draft_id=?", (draft_id,),
            ).fetchone():
                return {"ok": False, "draft_id": draft_id, "error": "Draft not found"}
            if arguments.get("_idempotency_key"):
                for existing in list_attachments(CONFIG.data_dir, draft_id):
                    existing_path = existing.get("path")
                    if (
                        existing.get("size") == len(data)
                        and existing_path
                        and Path(existing_path).read_bytes() == data
                    ):
                        return {
                            "ok": True, "draft_id": draft_id,
                            "filename": existing["filename"], "size": existing["size"],
                        }
            result = add_committed_draft_attachment(
                write_scope, data_dir=CONFIG.data_dir, draft_id=draft_id,
                filename=doc["filename"], data=data,
                actor_user_id=str(
                    (getattr(_execution_local, "execution_context", None) or {}).get("operator_id")
                    or "system:reference-attachment"
                ),
            )
        return {
            "ok": True, "draft_id": draft_id,
            "filename": result["filename"], "size": result["size"],
        }
    except (AttachmentError, WorkspaceLockedError) as exc:
        return {"ok": False, "draft_id": draft_id, "error": str(exc)}
    except ValueError as exc:
        if str(exc) in {"Draft not found", "Draft Workspace not initialized", "Attachment command requires an idle explicit connection"}:
            return {"ok": False, "draft_id": draft_id, "error": str(exc)}
        logger.exception("attach_reference_doc outcome uncertain for draft %s", draft_id)
        return {
            "ok": False, "draft_id": draft_id,
            "attachment_outcome": "unknown",
            "side_effect_status": "may_have_occurred",
            "error": "Attachment outcome unknown; inspect the draft before retrying.",
        }
    except Exception:
        # SQLite may have committed before the scope raised. Suppress gateway
        # retry and ask for verification rather than claiming an attachment.
        logger.exception("attach_reference_doc outcome uncertain for draft %s", draft_id)
        return {
            "ok": False, "draft_id": draft_id,
            "attachment_outcome": "unknown",
            "side_effect_status": "may_have_occurred",
            "error": "Attachment outcome unknown; inspect the draft before retrying.",
        }


def _handle_think(conn: sqlite3.Connection, arguments: dict[str, Any]) -> dict[str, Any]:
    from src.repositories.activity_log_repo import insert_activity
    reasoning = arguments.get("reasoning", "")
    summary = reasoning[:500]
    try:
        insert_activity(
            conn,
            actor="agent",
            action_type="agent_thinking",
            surface="chat",
            summary=summary,
        )
        # Note: no conn.commit() here — committing during an agent loop can block
        # 30+ seconds when the DB is busy, causing the asyncio timeout to fire.
        # The activity log row will be committed by the next write operation.
    except Exception:
        logger.debug("think: activity_log write failed (non-fatal)", exc_info=True)
    return {"acknowledged": "Noted. Continue with your next step."}


def _handle_load_project_context(conn: sqlite3.Connection, arguments: dict[str, Any]) -> dict[str, Any]:
    from src.context.layer1_truth import load_project_truth
    from src.context.layer2_working_memory import load_working_memory
    from src.context.layer3_relationship import load_relationship
    from src.context.layer4_evidence import load_evidence

    project_id = arguments["project_id"]
    layers = set(arguments.get("layers") or ["truth", "memory", "relationship", "evidence", "knowledge"])
    contact_id = arguments.get("contact_id")
    result: dict[str, Any] = {"project_id": project_id, "layers_loaded": sorted(layers)}

    if "truth" in layers:
        truth, _ = load_project_truth(conn, project_id)
        if truth is None:
            return {"error": f"Project {project_id} not found."}
        result["truth"] = _normalize_for_json(truth.model_dump())
    else:
        truth_obj = project_repo.get_project(conn, project_id)
        if truth_obj is None:
            return {"error": f"Project {project_id} not found."}
        truth = truth_obj

    if "memory" in layers:
        wm, _ = load_working_memory(conn, project_id)
        result["working_memory"] = _normalize_for_json(wm.model_dump()) if wm else {}
    if "relationship" in layers:
        from src.context.governed_memory import compatibility_memory_request
        context = getattr(_execution_local, "execution_context", {}) or {}
        request = compatibility_memory_request(conn, operator_id=context.get("operator_id"),
            project_id=project_id, contact_id=contact_id, surface="tool", task_type="load_project_context")
        rel, _ = load_relationship(conn, contact_id, project_id, memory_request=request)
        result["relationship"] = _normalize_for_json(rel.model_dump()) if rel else {}
    if "evidence" in layers:
        evidence_list, _ = load_evidence(conn, project_id)
        result["evidence"] = _normalize_for_json(evidence_list)
    if "knowledge" in layers:
        market = getattr(truth, "market", None)
        from src.context.knowledge_loader import load_knowledge_for_project
        kb = load_knowledge_for_project(market)
        result["knowledge"] = kb[:3000] if kb else ""
    return result


def _handle_wiki_search(conn: sqlite3.Connection, arguments: dict[str, Any]) -> dict[str, Any]:
    from src.wiki.indexer import wiki_search_hits, format_wiki_hits
    query = arguments.get("query", "")
    top_k = arguments.get("top_k", 5)
    domain = arguments.get("domain")

    # Log the same expanded, ranked and deduplicated pages delivered to the agent.
    # A separate raw search can rank different pages and corrupt learning signals.
    hits = wiki_search_hits(query, top_k=top_k, domain_filter=domain)
    results = format_wiki_hits(hits, query=query)
    context = getattr(_execution_local, "execution_context", {}) or {}
    source_sink = context.get("assistant_source_sink")
    if isinstance(source_sink, list):
        known = {str(item.get("source_id") or "") for item in source_sink if isinstance(item, dict)}
        for rank, hit in enumerate(hits, start=1):
            path = str(hit.get("file") or hit.get("path") or "").strip()
            if not path:
                continue
            source_id = f"wiki-{hashlib.sha256(path.encode('utf-8')).hexdigest()[:16]}"
            if source_id in known:
                continue
            source_sink.append({
                "type": "source",
                "source_id": source_id,
                "kind": "wiki",
                "title": str(hit.get("title") or path),
                "path": path,
                "rank": rank,
                "score": hit.get("score"),
                "query": query,
            })
            known.add(source_id)
    paths = list(dict.fromkeys(
        str(hit.get("file") or str(hit.get("path", "")).removesuffix(".md") + ".md")
        for hit in hits if hit.get("file") or hit.get("path")
    ))
    if paths:

        def _log_usage():
            try:
                with scoped_write() as wconn:
                    for path in paths:
                        wconn.execute(
                            "INSERT INTO wiki_usage_log (wiki_path) VALUES (?)",
                            (path,)
                        )
                    wconn.commit()
            except Exception:
                pass  # usage tracking is best-effort

        threading.Thread(target=_log_usage, daemon=True).start()

    return {"results": results, "query": query}


def _handle_delegate(conn: sqlite3.Connection, arguments: dict[str, Any]) -> dict[str, Any]:
    """Run a policy-controlled specialist and normalize boundary failures."""
    from config import defaults
    from src.agent.skills.registry import resolve_skill_or_specialist
    from src.agent.specialists.contracts import SpecialistReport, SpecialistType
    from src.agent.specialists.delegate_policy import (
        check_delegate_allowed,
        policy_error_payload,
        record_delegate_result,
    )
    from src.agent.specialists.runner import run_specialist_loop

    task = str(arguments.get("task") or "").strip()
    if not task:
        return {"ok": False, "error": "task is required", "summary": ""}
    project_id = arguments.get("project_id")
    skill_id = arguments.get("skill_id")
    resume_from = arguments.get("resume_from")
    progress_cb = getattr(_execution_local, "progress_cb", None)
    resolved_skill_id = str(skill_id or arguments.get("specialist_type") or "delegate")
    specialist_type = SpecialistType.RESEARCH
    try:
        specialist_type, skill = resolve_skill_or_specialist(
            skill_id=skill_id, specialist_type=arguments.get("specialist_type")
        )
        resolved_skill_id = skill.skill_id if skill is not None else (skill_id or specialist_type.value)
        if defaults.ORCHESTRATOR_MODE:
            allowed, message = check_delegate_allowed(
                skill_id=resolved_skill_id,
                resume_from=resume_from,
                specialist_type=specialist_type.value,
            )
            if not allowed:
                return policy_error_payload(message or "delegate blocked", skill_id=resolved_skill_id)

        if progress_cb is not None:
            from src.observability.tool_progress import format_sub_task_step
            emit_progress(progress_cb, format_sub_task_step(resolved_skill_id), "running", task)

        context = getattr(_execution_local, "execution_context", {}) or {}
        report = run_specialist_loop(
            conn, specialist_type, task, project_id=project_id, skill_id=resolved_skill_id,
            resume_from=resume_from, chat_id=get_active_chat_id(),
            operator_id=context.get("operator_id"),
            explicit_operator_instruction=bool(context.get("explicit_operator_instruction")),
            deterministic_authorization=bool(context.get("deterministic_authorization")),
            operator_request_text=context.get("operator_request_text"),
            read_only_execution=bool(context.get("read_only_execution")),
            dashboard_permissions=tuple(context.get("dashboard_permissions") or ()),
            deadline_monotonic=context.get("deadline_monotonic"),
            parent_tool_call_id=(
                str(context.get("tool_call_id"))
                if context.get("tool_call_id")
                else None
            ),
            shared_execution_context=context,
        )
        record_delegate_result(
            skill_id=resolved_skill_id, resume_from=resume_from,
            session_id=report.session_id, ok=report.ok,
            specialist_type=specialist_type.value,
        )
    except Exception as exc:
        logger.exception("delegate failed skill=%s", resolved_skill_id)
        report = SpecialistReport(
            specialist_type=specialist_type,
            skill_id=resolved_skill_id,
            ok=False,
            summary=f"Delegate failed: {exc}",
            error="delegate_failed",
            completion_status="failed",
            outcome="failed",
        )

    if progress_cb is not None:
        from src.observability.tool_progress import format_sub_task_step
        emit_progress(
            progress_cb, format_sub_task_step(resolved_skill_id),
            "done" if report.ok else "error", report.summary[:200],
        )
    return _normalize_for_json(report.model_dump())


def _learning_wiki_write_allowed(arguments: dict) -> dict | None:
    """Block LLM-originated Wiki writes. Only compiler and human may write."""
    from src.wiki.write_authority import WRITER_AGENT, wiki_write_denied_payload

    _ = arguments
    return wiki_write_denied_payload(WRITER_AGENT, operation="wiki_write")


def _handle_wiki_create(conn: sqlite3.Connection, arguments: dict) -> dict:
    if blocked := _learning_wiki_write_allowed(arguments):
        return blocked
    from datetime import datetime, timezone
    from src.wiki.schema import WikiPage, WikiLink, render_page
    from src.wiki.write_guard import WikiWriteGuard, WikiValidationError
    from src.wiki.graph import invalidate_wiki_graph
    from src.wiki.indexer import invalidate_wiki_index
    from src.repositories.activity_log_repo import insert_activity

    path_str = (arguments.get("path") or "").strip("/")
    if not path_str or "/" not in path_str:
        return {"error": "path must be 'category/stem', e.g. 'insight/my_page'"}

    category, stem = path_str.split("/", 1)
    out_path = _WIKI_DIR / category / f"{stem}.md"

    if out_path.exists():
        return {"error": f"Page '{path_str}' already exists. Use wiki_update to modify it."}

    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    raw_links = arguments.get("links") or []
    links = [
        WikiLink(target=lnk["target"], rel=lnk["rel"], note=lnk.get("note", ""))
        for lnk in raw_links
        if lnk.get("target") and lnk.get("rel")
    ]
    page = WikiPage(
        title=arguments.get("title", ""),
        type=arguments.get("page_type", ""),
        tags=arguments.get("tags") or [],
        sources=["agent_write"],
        created=today,
        updated=today,
        confidence=arguments.get("confidence", "low"),
        status="draft",
        links=links,
        body=arguments.get("body", ""),
    )

    guard = WikiWriteGuard()
    try:
        guard.validate(page, path=path_str)
    except WikiValidationError as e:
        return {"error": str(e)}

    out_path.parent.mkdir(parents=True, exist_ok=True)
    expected_content = render_page(page)
    out_path.write_text(expected_content, encoding="utf-8")
    persisted_content = out_path.read_text(encoding="utf-8")
    import hashlib
    revision = hashlib.sha256(persisted_content.encode("utf-8")).hexdigest()
    verified = persisted_content == expected_content
    if not verified:
        return {
            "success": False, "verified": False, "operation": "wiki_create",
            "record_id": path_str, "version": revision, "error": "Wiki write verification failed.",
        }

    insert_activity(conn, actor="agent", action_type="wiki_write",
                    summary=f"Created wiki page {path_str}: {page.title}")
    invalidate_wiki_graph()
    invalidate_wiki_index()

    contradictions = guard.check_contradictions(page)
    if contradictions:
        try:
            from src.telegram.card_push import push_error_card_sync
            push_error_card_sync(
                conn,
                task="wiki_contradiction",
                error_class="WikiContradiction",
                error_message=f"'{path_str}' contradicts {', '.join(contradictions)}",
            )
        except Exception:
            pass

    return {
        "success": True, "verified": True, "operation": "wiki_create",
        "record_id": path_str, "version": revision,
        "path": path_str, "title": page.title, "status": "created",
    }


def _handle_wiki_update(conn: sqlite3.Connection, arguments: dict) -> dict:
    if blocked := _learning_wiki_write_allowed(arguments):
        return blocked
    from datetime import datetime, timezone
    from src.wiki.schema import WikiLink, parse_page, render_page
    from src.wiki.write_guard import WikiWriteGuard, WikiValidationError
    from src.wiki.graph import invalidate_wiki_graph
    from src.wiki.indexer import invalidate_wiki_index
    from src.repositories.activity_log_repo import insert_activity

    path_str = (arguments.get("path") or "").strip("/")
    if not path_str or "/" not in path_str:
        return {"error": "path must be 'category/stem'"}

    category, stem = path_str.split("/", 1)
    file_path = _WIKI_DIR / category / f"{stem}.md"
    if not file_path.exists():
        return {"error": f"Page '{path_str}' does not exist. Use wiki_create to create it."}

    page = parse_page(file_path.read_text(encoding="utf-8"))

    if arguments.get("replace_body"):
        page.body = arguments["replace_body"]
    elif arguments.get("append_body"):
        page.body = (page.body or "").rstrip() + "\n\n" + arguments["append_body"]
    if arguments.get("confidence"):
        page.confidence = arguments["confidence"]

    raw_links = arguments.get("add_links") or []
    for lnk in raw_links:
        if lnk.get("target") and lnk.get("rel"):
            page.links.append(WikiLink(
                target=lnk["target"], rel=lnk["rel"], note=lnk.get("note", "")
            ))

    page.updated = datetime.now(timezone.utc).strftime("%Y-%m-%d")

    guard = WikiWriteGuard()
    try:
        guard.validate(page, path=path_str)
    except WikiValidationError as e:
        return {"error": str(e)}

    expected_content = render_page(page)
    file_path.write_text(expected_content, encoding="utf-8")
    persisted_content = file_path.read_text(encoding="utf-8")
    import hashlib
    revision = hashlib.sha256(persisted_content.encode("utf-8")).hexdigest()
    verified = persisted_content == expected_content
    if not verified:
        return {
            "success": False, "verified": False, "operation": "wiki_update",
            "record_id": path_str, "version": revision, "error": "Wiki write verification failed.",
        }
    insert_activity(conn, actor="agent", action_type="wiki_write",
                    summary=f"Updated wiki page {path_str}")
    invalidate_wiki_graph()
    invalidate_wiki_index()

    return {
        "success": True, "verified": True, "operation": "wiki_update",
        "record_id": path_str, "version": revision,
        "path": path_str, "title": page.title, "status": "updated",
    }


def _handle_wiki_read(conn: sqlite3.Connection, arguments: dict) -> dict:  # noqa: ARG001
    from src.wiki.schema import parse_page

    path_str = (arguments.get("path") or "").strip("/")
    if not path_str or "/" not in path_str:
        return {"error": "path must be 'category/stem', e.g. 'markets/jp_market'"}

    category, stem = path_str.split("/", 1)
    file_path = _WIKI_DIR / category / f"{stem}.md"
    if not file_path.exists():
        return {"error": f"Page '{path_str}' does not exist. Use wiki_search to find existing pages."}

    page = parse_page(file_path.read_text(encoding="utf-8"))
    return {
        "path": path_str,
        "title": page.title,
        "body": page.body or "",
        "confidence": page.confidence or "",
        "status": page.status or "",
        "updated": page.updated or "",
    }


def _handle_wiki_link(conn: sqlite3.Connection, arguments: dict) -> dict:
    if blocked := _learning_wiki_write_allowed(arguments):
        return blocked
    from datetime import datetime, timezone
    from src.wiki.schema import WikiLink, parse_page, render_page, VALID_LINK_RELS
    from src.wiki.write_guard import WikiWriteGuard, WikiValidationError
    from src.wiki.graph import invalidate_wiki_graph
    from src.wiki.indexer import invalidate_wiki_index
    from src.repositories.activity_log_repo import insert_activity

    from_str = (arguments.get("from_path") or "").strip("/")
    to_str = (arguments.get("to_path") or "").strip("/")
    rel = arguments.get("rel", "")

    if rel not in VALID_LINK_RELS:
        return {"error": f"Invalid rel '{rel}'. Must be one of: {sorted(VALID_LINK_RELS)}"}

    if not from_str or "/" not in from_str:
        return {"error": "from_path must be 'category/stem'"}

    cat, stem = from_str.split("/", 1)
    from_file = _WIKI_DIR / cat / f"{stem}.md"
    if not from_file.exists():
        return {"error": f"Source page '{from_str}' does not exist"}

    if not to_str or "/" not in to_str:
        return {"error": "to_path must be 'category/stem'"}
    to_cat, to_stem = to_str.split("/", 1)
    to_file = _WIKI_DIR / to_cat / f"{to_stem}.md"
    if not to_file.exists():
        return {"error": f"Target page '{to_str}' does not exist"}
    target_page = parse_page(to_file.read_text(encoding="utf-8"))
    if target_page.status == "archived":
        return {"error": f"Target page '{to_str}' is archived"}

    page = parse_page(from_file.read_text(encoding="utf-8"))
    note = arguments.get("note", "")
    existing = next(
        (link for link in page.links if link.target == to_str and link.rel == rel),
        None,
    )
    if existing is not None:
        if not existing.note and note:
            existing.note = note
        else:
            return {"from": from_str, "to": to_str, "rel": rel, "status": "no_change"}
    else:
        page.links.append(WikiLink(target=to_str, rel=rel, note=note))
    page.updated = datetime.now(timezone.utc).strftime("%Y-%m-%d")

    guard = WikiWriteGuard()
    try:
        guard.validate(page, path=from_str)
    except WikiValidationError as e:
        return {"error": str(e)}

    from_file.write_text(render_page(page), encoding="utf-8")
    insert_activity(conn, actor="agent", action_type="wiki_write",
                    summary=f"Linked {from_str} --[{rel}]--> {to_str}")
    invalidate_wiki_graph()
    invalidate_wiki_index()

    if rel == "contradicts":
        try:
            from src.telegram.card_push import push_error_card_sync
            push_error_card_sync(
                conn,
                task="wiki_contradiction",
                error_class="WikiContradiction",
                error_message=f"'{from_str}' contradicts '{to_str}'",
            )
        except Exception:
            pass

    return {"from": from_str, "to": to_str, "rel": rel, "status": "linked"}


def _handle_get_project_emails(conn: sqlite3.Connection, arguments: dict[str, Any]) -> dict[str, Any]:
    """Unified email timeline with canonical project scoping and metadata."""
    project_id = arguments.get("project_id")
    if not project_id:
        return {"emails": [], "total_count": 0, "error": "project_id is required"}
    limit = min(int(arguments.get("limit", 10)), 20)
    scope_sql, scope_params = _email_scope_sql(project_id)
    rows = conn.execute(
        f"""SELECT * FROM (
            SELECT ir.reply_id, ir.reply_id AS email_id, ir.received_at AS email_date,
                   ir.subject, ir.summary, ir.suggested_action, ir.confidence,
                   'inbound' AS direction, NULL AS sender_email, NULL AS cc,
                   ir.thread_id AS thread_id, NULL AS in_reply_to, NULL AS references_chain,
                   ir.project_id AS project_id, NULL AS body_snippet, 'project_id' AS project_match
              FROM inbound_replies ir WHERE ir.project_id = ?
            UNION ALL
            SELECT NULL AS reply_id, pe.email_id, pe.processed_at AS email_date,
                   pe.subject, NULL AS summary, NULL AS suggested_action, NULL AS confidence,
                   pe.direction, pe.sender_email, pe.cc, pe.thread_id, pe.in_reply_to,
                   pe.references_chain, pe.project_id, pe.body_snippet,
                   CASE WHEN pe.project_id = ? THEN 'project_id' ELSE 'subject_tag' END AS project_match
              FROM processed_emails pe WHERE {scope_sql}
        ) ORDER BY email_date DESC LIMIT ?""",
        [project_id, project_id, *scope_params, limit],
    ).fetchall()
    emails = [dict(row) for row in rows]
    for item in emails:
        item["body_preview"] = item.get("body_snippet") or ""
    return {"emails": emails, "total_count": len(emails)}


def _handle_get_email_body(conn: sqlite3.Connection, arguments: dict[str, Any]) -> dict[str, Any]:
    """Return a scoped email body with normalized Message-ID and thread metadata."""
    reply_id = arguments.get("reply_id")
    email_id = arguments.get("email_id")
    project_id = arguments.get("project_id")
    offset = int(arguments.get("offset", 0))

    if reply_id:
        row = conn.execute(
            "SELECT reply_id, project_id, body_text, thread_id, subject FROM inbound_replies WHERE reply_id = ?",
            (reply_id,),
        ).fetchone()
        if row:
            if project_id and row["project_id"] != project_id:
                return {"error": "project_scope_mismatch"}
            body = (row["body_text"] or "")[offset:]
            if len(body) > 4000:
                body = body[:4000] + f"\n[truncated - call again with offset={offset + 4000}]"
            return {"body_text": body, "reply_id": reply_id, "offset": offset,
                    "thread_id": row["thread_id"], "subject": row["subject"],
                    "project_match": "project_id" if project_id else None}
        if not email_id:
            return {"error": "email_not_found"}

    if email_id:
        row = _find_processed_email(conn, email_id)
        if row is None:
            return {"error": "email_not_found"}
        item = _with_project_match(row, project_id)
        if project_id:
            linked = item.get("project_id") == project_id
            tagged = item.get("project_id") is None and f"[{project_id}]" in (item.get("subject") or "")
            if not (linked or tagged):
                return {"error": "project_scope_mismatch"}
        body = (item.get("body_snippet") or "")[offset:]
        if len(body) > 4000:
            body = body[:4000] + f"\n[truncated - call again with offset={offset + 4000}]"
        return {
            "body_text": body, "email_id": item["email_id"],
            "direction": item.get("direction"), "sender_email": item.get("sender_email"),
            "subject": item.get("subject"), "cc": item.get("cc"),
            "thread_id": item.get("thread_id"), "in_reply_to": item.get("in_reply_to"),
            "references_chain": item.get("references_chain"),
            "project_match": item.get("project_match"), "offset": offset,
            "note": "body from processed_emails",
        }

    return {"error": "reply_id or email_id is required"}


# ── Flat dispatch dict ────────────────────────────────────────────

_CORE_TOOL_HANDLERS: dict[str, Any] = {
    # Read tools
    "get_project_truth": _handle_get_project_truth,
    "search_emails": _handle_search_emails,
    "get_working_memory": _handle_get_working_memory,
    "get_relationship_prefs": _handle_get_relationship_prefs,
    "find_internal_contact": _handle_find_internal_contact,
    "load_project_context": _handle_load_project_context,
    "wiki_search": _handle_wiki_search,
    "get_project_emails": _handle_get_project_emails,
    "get_email_body": _handle_get_email_body,
    "wiki_create": _handle_wiki_create,
    "wiki_read": _handle_wiki_read,
    "wiki_update": _handle_wiki_update,
    "wiki_link": _handle_wiki_link,
    # Write/action tools
    "draft_reply": _handle_draft_reply,
    "push_telegram_review": _handle_push_telegram_review,
    "log_evidence": _handle_log_evidence,
    # Meta tools
    "list_reference_docs": _handle_list_reference_docs,
    "attach_reference_doc": _handle_attach_reference_doc,
    "think": _handle_think,
    "delegate": _handle_delegate,
}

# Preserve source ownership so startup validation can detect duplicate keys before
# flat-map shadowing makes the conflict invisible.
TOOL_HANDLER_SOURCES: tuple[tuple[str, dict[str, Any]], ...] = (
    ("core", _CORE_TOOL_HANDLERS),
    ("assistant_workspace", ASSISTANT_WORKSPACE_TOOL_HANDLERS),
    ("chat", CHAT_TOOL_HANDLERS),
    ("project_intake", PROJECT_INTAKE_TOOL_HANDLERS),
    ("project_reconciliation", PROJECT_RECONCILIATION_TOOL_HANDLERS),
    ("calendar", CALENDAR_TOOL_HANDLERS),
    ("communication", COMMUNICATION_TOOL_HANDLERS),
    ("personal_work", PERSONAL_WORK_TOOL_HANDLERS),
    ("knowledge", KNOWLEDGE_TOOL_HANDLERS),
    ("evidence", EVIDENCE_TOOL_HANDLERS),
    ("artifact", ARTIFACT_TOOL_HANDLERS),
    ("support_chat", SUPPORT_CHAT_TOOL_HANDLERS),
    ("learning", LEARNING_TOOL_HANDLERS),
    ("work_item_chat", WORK_ITEM_CHAT_TOOL_HANDLERS),
    ("internal_action", INTERNAL_ACTION_TOOL_HANDLERS),
    ("fix_session", FIX_SESSION_TOOL_HANDLERS),
)

TOOL_HANDLERS: dict[str, Any] = {
    name: handler
    for _source_name, source_handlers in TOOL_HANDLER_SOURCES
    for name, handler in source_handlers.items()
}

# Alias for backward compatibility and test access
_TOOL_DISPATCH = TOOL_HANDLERS
_TOOL_GATEWAY = ToolExecutionGateway(
    lambda: TOOL_HANDLERS,
    lambda: TOOL_BY_NAME,
    health_tracker=get_tool_health_tracker(),
)


# ── Public API ────────────────────────────────────────────────────

_PARALLEL_SAFE_EFFECTS = frozenset({"none", "read"})
# ``delegate`` is registered as read-like so a read-only host can invoke read
# specialists, but a delegated specialist may perform staged/committed writes.
# Delegate policy accounting is sequential, so never execute delegates in a
# parallel batch even though the outer orchestration call is read-like.
_PARALLEL_BLOCKED_TOOLS = frozenset({"delegate"})
_SELF_SCOPED_WRITE_TOOLS = frozenset({"draft_reply", "ingest_meeting_notes", "attach_reference_doc"})


def _tool_needs_write(tool_name: str) -> bool:
    """Return whether a registered tool requires a write-capable connection."""
    return _TOOL_GATEWAY.needs_write(tool_name)


def _publish_tool_receipt(
    *,
    receipt_sink: Any,
    tool_call_id: str,
    tool_name: str,
    result: Any,
    project_id: str | None,
) -> ToolExecutionReceipt:
    """Publish one authoritative outcome and optionally retain its receipt."""
    receipt = receipt_from_result(
        tool_call_id=tool_call_id,
        tool_name=tool_name,
        result=result,
        project_id=project_id,
    )
    get_tool_health_tracker().record_outcome(
        tool_name,
        status=receipt.status,
        error_code=receipt.error_code,
    )
    if receipt_sink is not None:
        receipt_sink(receipt)
    return receipt


def _connection_is_query_only(conn: sqlite3.Connection | None) -> bool:
    """Return True when SQLite query_only is enabled on the connection."""
    if conn is None:
        return False
    try:
        row = conn.execute("PRAGMA query_only").fetchone()
        value = row[0] if row is not None else 0
        return bool(int(value))
    except (AttributeError, IndexError, TypeError, ValueError, sqlite3.Error):
        return False


def _all_read_only(tool_calls: list[dict[str, Any]]) -> bool:
    """Return True iff batch has >1 tool and all are read-only or side-effect-free.

    Inspired by Hermes' _should_parallelize_tool_batch() pattern.
    Only parallelises when there is actual benefit (>1 tool) and no risk of
    write conflicts.
    """
    if len(tool_calls) <= 1:
        return False
    for tc in tool_calls:
        name = tc.get("function", {}).get("name", "")
        contract = TOOL_BY_NAME.get(name)
        if contract is None:
            return False  # Unknown tool — don't parallelize
        if name in _PARALLEL_BLOCKED_TOOLS:
            return False
        if contract.side_effect_type not in _PARALLEL_SAFE_EFFECTS:
            return False
    return True


def execute_tool(
    conn: sqlite3.Connection,
    task_type: str,
    tool_name: str,
    arguments: dict[str, Any],
    *,
    execution_context: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Execute one tool call and emit privacy-safe operational metrics."""
    contract, _handler = _TOOL_GATEWAY.resolve(tool_name)
    timeout = contract.timeout_seconds
    start = time.monotonic()
    tracer = trace.get_tracer("agentfixbpro.tools")
    with tracer.start_as_current_span("tool.execute") as span:
        span.set_attribute("tool.name", tool_name)
        span.set_attribute("tool.task_type", task_type)
        span.set_attribute("tool.timeout_seconds", timeout)
        span.set_attribute("tool.side_effect_type", str(contract.side_effect_type if contract else "unknown"))
        context = dict(getattr(_execution_local, "execution_context", None) or {})
        context.update(execution_context or {})
        # Caller-supplied loop context cannot remove an active inbound policy.
        from src.email.analysis_execution import active_analysis_policy
        active_policy = active_analysis_policy()
        if active_policy is not None:
            context["inbound_analysis_policy"] = active_policy
        trace_context = current_trace_context()
        tool_call_id = str(context.get("tool_call_id") or new_identifier("tool"))
        context["tool_call_id"] = tool_call_id
        if trace_context is not None:
            emit_event(
                trace_context,
                "tool.start",
                tool_call_id=tool_call_id,
                status="running",
                metadata={
                    "tool_name": tool_name,
                    "task_type": task_type,
                    "timeout_seconds": timeout,
                    "side_effect_type": str(contract.side_effect_type if contract else "unknown"),
                },
                payload={"arguments": arguments},
            )
        try:
            result = _TOOL_GATEWAY.execute(
                conn,
                task_type=task_type,
                tool_name=tool_name,
                arguments=arguments,
                execution_context=context,
                context_binder=trusted_execution_context,
            )
            result_sink = context.get("assistant_tool_result_sink")
            if isinstance(result_sink, list):
                result_sink.append({
                    "tool": tool_name,
                    "arguments": _normalize_for_json(arguments),
                    "result": _normalize_for_json(result),
                })
            normalized = _normalize_for_json(result)
            outcome_receipt = receipt_from_result(
                tool_call_id=tool_call_id,
                tool_name=tool_name,
                result=result,
            )
            returned_failure = outcome_receipt.status != "succeeded"
            encoded = json.dumps(normalized, default=str, ensure_ascii=False).encode("utf-8")
            row_count = 0
            if isinstance(result, dict):
                for key in ("rows", "items", "projects", "results", "records"):
                    value = result.get(key)
                    if isinstance(value, list):
                        row_count = max(row_count, len(value))
            span.set_attribute("tool.result_bytes", len(encoded))
            span.set_attribute("tool.result_rows", row_count)
            span.set_attribute("tool.outcome_status", outcome_receipt.status)
            span.set_attribute("tool.returned_failure", returned_failure)
            if outcome_receipt.error_code:
                span.set_attribute("tool.error_code", outcome_receipt.error_code)
            if returned_failure:
                span.set_status(Status(StatusCode.ERROR, outcome_receipt.outcome))
            if trace_context is not None:
                emit_event(
                    trace_context,
                    "tool.end",
                    tool_call_id=tool_call_id,
                    status="error" if returned_failure else "ok",
                    duration_ms=(time.monotonic() - start) * 1000,
                    metadata={
                        "tool_name": tool_name,
                        "task_type": task_type,
                        "result_bytes": len(encoded),
                        "row_count": row_count,
                        "outcome_status": outcome_receipt.status,
                        "outcome": outcome_receipt.outcome,
                        "error_code": outcome_receipt.error_code,
                        "circuit_blocked": (
                            outcome_receipt.error_code == "tool_circuit_open"
                        ),
                    },
                    payload={"result": normalized},
                )
            return result
        except (ToolContractValidationError, ContextConfidenceError) as exc:
            span.record_exception(exc)
            span.set_status(Status(StatusCode.ERROR, type(exc).__name__))
            span.set_attribute("tool.error_type", type(exc).__name__)
            if trace_context is not None:
                emit_event(
                    trace_context,
                    "tool.error",
                    tool_call_id=tool_call_id,
                    status="error",
                    duration_ms=(time.monotonic() - start) * 1000,
                    metadata={
                        "tool_name": tool_name,
                        "task_type": task_type,
                        "error_class": type(exc).__name__,
                        "error_code": exc.code,
                    },
                    payload={"error": str(exc)},
                )
            return exc.as_result()
        except Exception as exc:
            span.record_exception(exc)
            span.set_status(Status(StatusCode.ERROR, type(exc).__name__))
            span.set_attribute("tool.error_type", type(exc).__name__)
            if trace_context is not None:
                emit_event(
                    trace_context,
                    "tool.error",
                    tool_call_id=tool_call_id,
                    status="error",
                    duration_ms=(time.monotonic() - start) * 1000,
                    metadata={
                        "tool_name": tool_name,
                        "task_type": task_type,
                        "error_class": type(exc).__name__,
                    },
                    payload={"arguments": arguments, "error": str(exc)},
                )
            raise
        finally:
            elapsed = time.monotonic() - start
            span.set_attribute("tool.duration_ms", round(elapsed * 1000, 3))
            span.set_attribute("tool.timed_out", elapsed > timeout)
            if elapsed > timeout:
                logger.warning(
                    "Tool %s exceeded contract timeout (%ds, actual %.1fs)",
                    tool_name,
                    timeout,
                    elapsed,
                )

def execute_tool_calls(
    conn: sqlite3.Connection | None,
    task_type: str,
    tool_calls: list[dict[str, Any]],
    *,
    progress_cb: ProgressCallback | None = None,
    operator_id: str | None = None,
    digest_tool_results: bool = False,
    thread_id: str | None = None,
    project_id: str | None = None,
    chat_id: int | None = None,
    allowed_tool_names: set[str] | frozenset[str] | None = None,
    blocked_tool_names: set[str] | frozenset[str] | None = None,
    receipt_sink: Any = None,
    execution_context: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    """Execute tool calls and return tool-role messages for conversation replay.

    When all tools in the batch are read-only or side-effect-free, runs them
    in parallel via ThreadPoolExecutor. Falls back to sequential on any error.
    """
    previous_chat_id = getattr(_execution_local, "chat_id", None)
    previous_progress_cb = getattr(_execution_local, "progress_cb", None)
    previous_execution_context = getattr(_execution_local, "execution_context", None)
    _execution_local.chat_id = chat_id
    _execution_local.progress_cb = progress_cb
    merged_context = dict(getattr(_execution_local, "execution_context", None) or {})
    merged_context.update(execution_context or {})
    from src.email.analysis_execution import active_analysis_policy
    active_policy = active_analysis_policy()
    if active_policy is not None:
        merged_context["inbound_analysis_policy"] = active_policy
    if operator_id is not None:
        merged_context["operator_id"] = operator_id
    if chat_id is not None:
        merged_context["chat_id"] = chat_id
    if allowed_tool_names is not None:
        merged_context["runtime_allowed_tool_names"] = frozenset(allowed_tool_names)
    if blocked_tool_names is not None:
        merged_context["blocked_tool_names"] = frozenset(blocked_tool_names)
    _execution_local.execution_context = merged_context
    filtered_calls = [
        call for call in tool_calls
        if allowed_tool_names is None
        or call.get("function", {}).get("name", "") in allowed_tool_names
    ]
    try:
        # An injected connection is authoritative. SQLite connections cannot
        # be shared safely across the worker threads used below, so run these
        # calls sequentially rather than silently opening default connections.
        if conn is None and _all_read_only(filtered_calls):
            messages = _execute_parallel(
                conn, task_type, filtered_calls, progress_cb, merged_context,
                operator_id=operator_id, digest_tool_results=digest_tool_results,
                thread_id=thread_id, project_id=project_id, receipt_sink=receipt_sink,
            )
        else:
            messages = _execute_sequential(
                conn, task_type, filtered_calls, progress_cb, merged_context,
                operator_id=operator_id, digest_tool_results=digest_tool_results,
                thread_id=thread_id, project_id=project_id, receipt_sink=receipt_sink,
            )
        return messages
    finally:
        _execution_local.chat_id = previous_chat_id
        _execution_local.progress_cb = previous_progress_cb
        _execution_local.execution_context = previous_execution_context


def _execute_owned_project_update_tool(
    writer_factory: Callable,
    task_type: str,
    tool_name: str,
    arguments: dict[str, Any],
    call_context: dict[str, Any],
) -> dict[str, Any]:
    """Finish a project command under one scoped writer, then run its effects."""
    effects: list[Callable[[], None]] = []
    owned_context = {
        **call_context,
        "owned_write_scope": True,
        "post_commit_effects": effects,
    }
    result = None
    try:
        with writer_factory() as wconn:
            result = execute_tool(
                wconn, task_type, tool_name, arguments,
                execution_context=owned_context,
            )
            if not isinstance(result, dict) or result.get("side_effect_status") == "may_have_occurred":
                raise RuntimeError(f"{tool_name} returned an uncertain write outcome")
        if wconn.in_transaction:
            raise RuntimeError(f"{tool_name} write scope did not commit its transaction")
    except Exception as exc:
        if (isinstance(result, dict) and result.get("ok") is True
                and not result.get("operation_id") and result.get("outcome") in {
                    "accepted", "no_change", "cancelled",
                }):
            # An accepted handler result is not the scoped owner's commit receipt.
            # For genuine untyped legacy work, exit can raise before COMMIT or
            # after it. Do not infer rollback, replay work, publish queued effects,
            # or invent an operation; typed-operation outcomes stay unchanged.
            logger.exception("%s writer exit failed; commit outcome unknown", tool_name)
            unknown = {
                "ok": False, "committed": False, "outcome": "unknown",
                "side_effect_status": "may_have_occurred",
                "error_code": "tool_execution_exception", "error": str(exc),
                "text": (
                    "Tool write outcome unknown. Do not retry automatically. "
                    "Inspect the persisted state before further action."
                ),
            }
            if result.get("project_id"):
                unknown["project_id"] = result["project_id"]
            return unknown
        raise
    # No external effects or durable receipt are claimed if the scope failed.
    if result.get("ok") is True and result.get("outcome") in {
        "accepted", "no_change", "cancelled",
    }:
        result["committed"] = True
    for effect in effects:
        try:
            effect()
        except Exception:
            logger.exception("Committed %s post-commit effect failed", tool_name)
    return result


def _execute_owned_reconciliation_tool(
    writer_factory: Callable,
    task_type: str,
    arguments: dict[str, Any],
    call_context: dict[str, Any],
) -> dict[str, Any]:
    """Stage in our writer, then publish and read the durable receipt."""
    from src.project_reconciliation.repository import get_operation
    from src.project_reconciliation.service_support import stored_result

    effects: list[Callable[[], None]] = []
    with writer_factory() as wconn:
        result = execute_tool(
            wconn, task_type, "project_reconcile", arguments,
            execution_context={**call_context, "post_commit_effects": effects},
        )
        if not isinstance(result, dict) or result.get("side_effect_status") == "may_have_occurred":
            raise RuntimeError("project_reconcile returned an uncertain write outcome")
    # A failed writer exit never reaches effects or the durable receipt.
    if wconn.in_transaction:
        raise RuntimeError("project_reconcile write scope did not commit its transaction")
    for effect in effects:
        try:
            effect()
        except Exception:
            logger.exception("Committed project_reconcile post-commit effect failed")
    if result.get("status") == "pending_commit":
        from src.db.connection import scoped_read

        operation_id = result.get("operation_id")
        try:
            with scoped_read() as rconn:
                operation = get_operation(rconn, operation_id) if operation_id else None
        except Exception:
            logger.exception("Could not verify committed project_reconcile receipt")
            operation = None
        durable = stored_result(operation) if operation is not None else None
        if (durable is None or durable.operation_id != operation_id
                or durable.project_id != result.get("project_id") or not durable.committed):
            return {
                **result, "ok": False, "committed": False, "status": "unknown",
                "outcome": "unknown", "error_code": "durable_receipt_unavailable",
                "side_effect_status": "may_have_occurred",
                "error": "Reconciliation outcome could not be verified after the write scope.",
                "text": (
                    "Reconciliation outcome unknown: the durable receipt could not be "
                    "verified. Do not retry automatically. Manually inspect operation "
                    f"{operation_id} and the project state before further action."
                ),
            }
        return durable.model_dump(mode="json")
    return result


def _unknown_stage_commit_result(staged_result: dict[str, Any]) -> dict[str, Any]:
    """A failed writer acknowledgement cannot prove rollback or durability."""
    return {
        "ok": False, "committed": False, "outcome": "unknown",
        "side_effect_status": "may_have_occurred",
        "error_code": "stage_commit_uncertain",
        "operation_id": staged_result["operation_id"],
        "project_id": staged_result.get("project_id"),
        "error": "Stage write outcome unknown; inspect the pending operation before retrying.",
        "text": (
            "Stage outcome unknown. Do not retry automatically. "
            "Inspect the pending operation and card intent before taking another action."
        ),
    }


def _unknown_injected_commit_result() -> dict[str, Any]:
    """A raised commit on an injected writer proves neither rollback nor durability."""
    return {
        "ok": False, "committed": False, "outcome": "unknown",
        "side_effect_status": "may_have_occurred",
        "error_code": "injected_tool_commit_uncertain",
        "error": "Tool write outcome unknown; inspect the persisted state before retrying.",
        "text": (
            "Tool write outcome unknown. Do not retry automatically. "
            "Inspect the persisted state before further action."
        ),
    }


def _execute_sequential(
    conn: sqlite3.Connection | None,
    task_type: str,
    tool_calls: list[dict[str, Any]],
    progress_cb: ProgressCallback | None,
    execution_context: dict[str, Any] | None,
    *,
    operator_id: str | None = None,
    digest_tool_results: bool = False,
    thread_id: str | None = None,
    project_id: str | None = None,
    receipt_sink: Any = None,
) -> list[dict[str, Any]]:
    """Original sequential execution path.

    When *conn* is ``None``, each tool call gets its own short-lived
    connection — ``scoped_read()`` for read-only tools, ``scoped_write()``
    for tools with side effects.  No write lock held between tools.
    """
    results: list[dict[str, Any]] = []
    for tool_call in tool_calls:
        function_data = tool_call.get("function", {})
        tool_name = function_data.get("name", "")
        raw_args = function_data.get("arguments") or "{}"
        step_name = f"Execute tool: {tool_name or 'unknown'}"
        # An injected connection may already have caller-owned work. A tool
        # must not commit that transaction merely to release its own lock.
        caller_transaction = conn is not None and conn.in_transaction
        tool_invoked = False
        staged_writer_result: dict[str, Any] | None = None
        emit_progress(progress_cb, step_name, "running", "Executing tool request")
        try:
            arguments = json.loads(raw_args) if isinstance(raw_args, str) else dict(raw_args)
        except json.JSONDecodeError as exc:
            tool_result = {
                "ok": False,
                "error": f"Invalid tool arguments: {exc}",
                "error_code": "tool_arguments_invalid_json",
                "side_effect_status": "not_started",
            }
            emit_progress(progress_cb, step_name, "error", "Tool arguments could not be parsed")
        else:
            call_context = dict(execution_context or {})
            call_context["tool_call_id"] = tool_call.get("id", "")
            try:
                tool_invoked = True
                if tool_name in _SELF_SCOPED_WRITE_TOOLS:
                    if conn is not None:
                        tool_result = execute_tool(
                            conn, task_type, tool_name, arguments,
                            execution_context=call_context,
                        )
                    else:
                        from src.db.connection import scoped_read
                        with scoped_read() as rconn:
                            tool_result = execute_tool(
                                rconn, task_type, tool_name, arguments,
                                execution_context=call_context,
                            )
                elif (tool_name == "project_reconcile" and caller_transaction
                      and _connection_is_query_only(conn)):
                    # An injected transaction is never replaced by a new writer.
                    # A query-only scope cannot stage a commit, even if the caller
                    # provided an effects list.
                    tool_result = execute_tool(
                        conn, task_type, tool_name, arguments,
                        execution_context={**call_context, "post_commit_effects": None},
                    )
                elif conn is not None and not (
                    _tool_needs_write(tool_name) and _connection_is_query_only(conn)
                ):
                    tool_result = execute_tool(
                        conn, task_type, tool_name, arguments,
                        execution_context=call_context,
                    )
                elif conn is not None:
                    writer_factory = call_context.get("write_connection_factory")
                    if writer_factory is None:
                        from src.db.connection import scoped_write
                        writer_factory = scoped_write
                    if tool_name in {"confirm_update", "cancel_update"}:
                        tool_result = _execute_owned_project_update_tool(
                            writer_factory, task_type, tool_name, arguments, call_context,
                        )
                    elif tool_name == "project_reconcile":
                        tool_result = _execute_owned_reconciliation_tool(
                            writer_factory, task_type, arguments, call_context,
                        )
                    else:
                        with writer_factory() as wconn:
                            tool_result = execute_tool(
                                wconn, task_type, tool_name, arguments,
                                execution_context=call_context,
                            )
                            if (tool_name in {"update_project", "update_project_batch"}
                                    and isinstance(tool_result, dict)
                                    and tool_result.get("outcome") == "staged"
                                    and tool_result.get("operation_id")):
                                staged_writer_result = dict(tool_result)
                else:
                    # Use scoped_read for read-only tools, scoped_write for writers.
                    contract = TOOL_BY_NAME.get(tool_name)
                    is_read_only = (
                        contract is not None
                        and contract.side_effect_type in _PARALLEL_SAFE_EFFECTS
                    )
                    if is_read_only:
                        from src.db.connection import scoped_read
                        with scoped_read() as rconn:
                            tool_result = execute_tool(
                                rconn, task_type, tool_name, arguments,
                                execution_context=call_context,
                            )
                    else:
                        from src.db.connection import scoped_write
                        if tool_name in {"confirm_update", "cancel_update"}:
                            tool_result = _execute_owned_project_update_tool(
                                scoped_write, task_type, tool_name, arguments, call_context,
                            )
                        elif tool_name == "project_reconcile":
                            tool_result = _execute_owned_reconciliation_tool(
                                scoped_write, task_type, arguments, call_context,
                            )
                        else:
                            with scoped_write() as wconn:
                                tool_result = execute_tool(
                                    wconn, task_type, tool_name, arguments,
                                    execution_context=call_context,
                                )
                                if (tool_name in {"update_project", "update_project_batch"}
                                        and isinstance(tool_result, dict)
                                        and tool_result.get("outcome") == "staged"
                                        and tool_result.get("operation_id")):
                                    staged_writer_result = dict(tool_result)
            except (ToolContractValidationError, ContextConfidenceError) as exc:
                tool_result = exc.as_result()
                emit_progress(progress_cb, step_name, "error", str(exc))
            except PermissionError as exc:
                tool_result = {
                    "ok": False,
                    "status": "denied",
                    "error": str(exc),
                    "error_code": "tool_authorization_denied",
                    "side_effect_status": "not_started",
                }
                emit_progress(progress_cb, step_name, "error", str(exc))
            except Exception as exc:
                if staged_writer_result is not None:
                    # The writer may have committed before its exit failed. The
                    # operation ID is an inspection hint, not a durable receipt.
                    tool_result = _unknown_stage_commit_result(staged_writer_result)
                    logger.exception("Stage writer exit failed; operation outcome unknown")
                else:
                    tool_result = {
                        "ok": False,
                        "error": str(exc),
                        "error_code": "tool_execution_exception",
                    }
                emit_progress(progress_cb, step_name, "error", str(tool_result["error"]))
            else:
                # An idle injected writer has not proved durability until commit returns.
                if isinstance(tool_result, dict) and tool_result.get("error"):
                    emit_progress(progress_cb, step_name, "error", str(tool_result.get("error")))
                elif not (
                    conn is not None and not caller_transaction
                    and (
                        _tool_needs_write(tool_name)
                        or (tool_name in {"update_project", "update_project_batch"}
                            and isinstance(tool_result, dict)
                            and tool_result.get("outcome") == "staged")
                    )
                ):
                    emit_progress(progress_cb, step_name, "done", "Tool completed successfully")
            # Only release an implicit transaction opened by this call on an
            # otherwise idle injected connection. A pre-existing transaction
            # belongs to the caller, even on invalid arguments or exceptions.
            # Normal runtime calls use conn=None and self-scoped connections.
            if conn is not None and tool_invoked and not caller_transaction:
                try:
                    conn.commit()
                except Exception:
                    if (tool_name in {"update_project", "update_project_batch"}
                            and isinstance(tool_result, dict)
                            and tool_result.get("outcome") == "staged"
                            and tool_result.get("operation_id")):
                        tool_result = _unknown_stage_commit_result(tool_result)
                        logger.exception("Injected stage commit failed; operation outcome unknown")
                        emit_progress(progress_cb, step_name, "error", str(tool_result["error"]))
                    elif _tool_needs_write(tool_name):
                        tool_result = _unknown_injected_commit_result()
                        logger.exception("Injected tool commit failed; write outcome unknown")
                        emit_progress(progress_cb, step_name, "error", str(tool_result["error"]))
                else:
                    if (tool_name in {"update_project", "update_project_batch"}
                            and isinstance(tool_result, dict)
                            and tool_result.get("outcome") == "staged"):
                        emit_progress(progress_cb, step_name, "done", "Tool completed successfully")
                    elif _tool_needs_write(tool_name) and not (
                        isinstance(tool_result, dict) and tool_result.get("error")
                    ):
                        emit_progress(progress_cb, step_name, "done", "Tool completed successfully")
        _publish_tool_receipt(
            receipt_sink=receipt_sink,
            tool_call_id=tool_call.get("id", ""),
            tool_name=tool_name,
            result=tool_result,
            project_id=project_id,
        )
        display_result = tool_result
        if digest_tool_results:
            from src.agent.tool_digest import digest_tool_result
            display_result = digest_tool_result(
                tool_name, tool_result, conn=conn, operator_id=operator_id,
                thread_id=thread_id, project_id=project_id,
            )
        results.append({
            "role": "tool",
            "tool_call_id": tool_call.get("id", ""),
            "content": json.dumps(_normalize_for_json(display_result), default=str, ensure_ascii=False),
        })
    return results


def _execute_parallel(
    conn: sqlite3.Connection,
    task_type: str,
    tool_calls: list[dict[str, Any]],
    progress_cb: ProgressCallback | None,
    execution_context: dict[str, Any] | None,
    *,
    operator_id: str | None = None,
    digest_tool_results: bool = False,
    thread_id: str | None = None,
    project_id: str | None = None,
    receipt_sink: Any = None,
) -> list[dict[str, Any]]:
    """Parallel execution for read-only tool batches.

    Each worker gets its own scoped_read() connection so we never share a
    single sqlite3 connection across threads. Results returned in original
    call order.
    """
    from src.db.connection import scoped_read

    logger.debug("Parallel tool execution: %d tools", len(tool_calls))

    def _run_one(index: int, tool_call: dict) -> tuple[int, dict]:
        function_data = tool_call.get("function", {})
        tool_name = function_data.get("name", "")
        raw_args = function_data.get("arguments") or "{}"
        step_name = f"Execute tool: {tool_name or 'unknown'}"
        try:
            arguments = json.loads(raw_args) if isinstance(raw_args, str) else dict(raw_args)
        except json.JSONDecodeError as exc:
            try:
                emit_progress(progress_cb, step_name, "error", str(exc))
            except Exception:
                pass
            return index, {
                "ok": False,
                "error": f"Invalid tool arguments: {exc}",
                "error_code": "tool_arguments_invalid_json",
                "side_effect_status": "not_started",
            }
        try:
            call_context = dict(execution_context or {})
            call_context["tool_call_id"] = tool_call.get("id", "")
            with scoped_read() as thread_conn:
                result = execute_tool(
                    thread_conn, task_type, tool_name, arguments,
                    execution_context=call_context,
                )
        except (ToolContractValidationError, ContextConfidenceError) as exc:
            try:
                emit_progress(progress_cb, step_name, "error", str(exc))
            except Exception:
                pass
            return index, exc.as_result()
        except PermissionError as exc:
            try:
                emit_progress(progress_cb, step_name, "error", str(exc))
            except Exception:
                pass
            return index, {
                "ok": False,
                "status": "denied",
                "error": str(exc),
                "error_code": "tool_authorization_denied",
                "side_effect_status": "not_started",
            }
        except Exception as exc:
            try:
                emit_progress(progress_cb, step_name, "error", str(exc))
            except Exception:
                pass
            return index, {
                "ok": False,
                "error": str(exc),
                "error_code": "tool_execution_exception",
            }
        try:
            emit_progress(progress_cb, step_name, "done", "Tool completed successfully")
        except Exception:
            pass
        return index, result

    ordered: list[dict | None] = [None] * len(tool_calls)
    try:
        with ThreadPoolExecutor(max_workers=min(len(tool_calls), 4)) as pool:
            futures = {
                pool.submit(copy_context().run, _run_one, i, tc): i
                for i, tc in enumerate(tool_calls)
            }
            for future in as_completed(futures):
                idx, tool_result = future.result()
                ordered[idx] = tool_result
    except Exception:
        logger.warning("Parallel tool execution failed, falling back to sequential", exc_info=True)
        return _execute_sequential(
            conn, task_type, tool_calls, progress_cb, execution_context,
            operator_id=operator_id, digest_tool_results=digest_tool_results,
            thread_id=thread_id, project_id=project_id, receipt_sink=receipt_sink,
        )

    results = []
    for tool_call, tool_result in zip(tool_calls, ordered):
        if tool_result is None:
            tool_result = {"error": "parallel execution slot missing result"}
        tool_name = tool_call.get("function", {}).get("name", "")
        _publish_tool_receipt(
            receipt_sink=receipt_sink,
            tool_call_id=tool_call.get("id", ""),
            tool_name=tool_name,
            result=tool_result,
            project_id=project_id,
        )
        display_result = tool_result
        if digest_tool_results:
            from src.agent.tool_digest import digest_tool_result
            display_result = digest_tool_result(
                tool_name, tool_result, conn=conn, operator_id=operator_id,
                thread_id=thread_id, project_id=project_id,
            )
        results.append({
            "role": "tool",
            "tool_call_id": tool_call.get("id", ""),
            "content": json.dumps(_normalize_for_json(display_result), default=str, ensure_ascii=False),
        })
    return results


# ── Serialization helpers ─────────────────────────────────────────

def _normalize_for_json(value: Any) -> Any:
    if isinstance(value, BaseModel):
        return _normalize_for_json(value.model_dump())
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, dict):
        return {key: _normalize_for_json(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_normalize_for_json(item) for item in value]
    return value


def _parse_json_list(raw: Any) -> list[str]:
    if not raw:
        return []
    if isinstance(raw, list):
        return [str(item) for item in raw]
    if isinstance(raw, str):
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError:
            return [part.strip() for part in raw.split(",") if part.strip()]
        if isinstance(parsed, list):
            return [str(item) for item in parsed]
    return []
