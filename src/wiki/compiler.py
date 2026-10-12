"""Nightly wiki compiler: turn eligible nuggets into draft pages."""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from config.settings import BASE_DIR

logger = logging.getLogger(__name__)

_WIKI_DIR = BASE_DIR / "wiki"


def enqueue_nugget_for_compile(conn, row: dict[str, Any]) -> str | None:
    """Classify one knowledge-log row and enqueue it. Returns queue_id."""
    from src.repositories.wiki_compiler_repo import enqueue_nugget
    from src.wiki.compiler_policy import classify_row

    entry_id = str(row.get("entry_id") or "")
    if not entry_id:
        return None
    topic, skip_reason = classify_row(row)
    state = "skipped" if skip_reason else "pending"
    return enqueue_nugget(
        conn,
        entry_id=entry_id,
        domain=row.get("domain"),
        topic=topic,
        state=state,
        skip_reason=skip_reason,
    )


def enqueue_unqueued_nuggets(conn, *, limit: int = 200) -> dict[str, int]:
    """Backfill the queue from email_knowledge_log rows not yet queued."""
    rows = conn.execute(
        """SELECT e.entry_id, e.title, e.content, e.confidence, e.domain, e.knowledge_type
           FROM email_knowledge_log e
           LEFT JOIN wiki_compiler_queue q ON q.entry_id = e.entry_id
           WHERE q.queue_id IS NULL
           ORDER BY e.created_at ASC
           LIMIT ?""",
        (limit,),
    ).fetchall()
    queued = skipped = 0
    for raw in rows:
        row = dict(raw)
        queue_id = enqueue_nugget_for_compile(conn, row)
        if not queue_id:
            continue
        state = conn.execute(
            "SELECT state FROM wiki_compiler_queue WHERE queue_id = ?",
            (queue_id,),
        ).fetchone()
        if state and state[0] == "skipped":
            skipped += 1
        else:
            queued += 1
    return {"queued": queued, "skipped": skipped, "seen": len(rows)}


def _compile_messages(nuggets: list[dict[str, Any]], catalog: str) -> list[dict[str, str]]:
    import json

    prompt_path = BASE_DIR / "prompts" / "wiki_curator.md"
    system = (
        prompt_path.read_text(encoding="utf-8")
        if prompt_path.exists()
        else "You are a wiki curator."
    )
    payload = [
        {
            "entry_id": item.get("entry_id"),
            "title": item.get("title"),
            "content": item.get("content"),
            "knowledge_type": item.get("knowledge_type"),
            "domain": item.get("domain"),
            "sources": item.get("source_emails"),
        }
        for item in nuggets
    ]
    user = (
        "## Current Wiki Catalog\n"
        f"{catalog}\n\n"
        "## Knowledge nuggets to compile\n"
        "These are reusable facts already extracted from email. "
        "Prefer update of an existing catalog page. "
        "Do not create account-lifecycle, holiday-calendar, or BAU ops pages. "
        "Write status: draft. Keep pages durable and enrich-first.\n\n"
        + json.dumps({"nuggets": payload}, ensure_ascii=False, indent=2)
    )
    return [
        {"role": "system", "content": system},
        {"role": "user", "content": user},
    ]


def _call_compiler_llm(nuggets: list[dict[str, Any]], catalog: str) -> list:
    from config.defaults import WIKI_CURATOR_LLM_TIMEOUT_SEC, WIKI_CURATOR_OUTPUT_RESERVE
    from src.llm.client import _trim_messages_to_fit, llm_call_structured

    schema = {
        "type": "object",
        "properties": {
            "operations": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "action": {"type": "string"},
                        "path": {"type": "string"},
                        "content": {"type": "string"},
                    },
                },
            },
        },
    }
    messages = _trim_messages_to_fit(
        _compile_messages(nuggets, catalog),
        reserve_output=WIKI_CURATOR_OUTPUT_RESERVE,
    )
    result = llm_call_structured(
        "wiki_curator",
        messages,
        schema,
        temperature=0.3,
        timeout_sec=WIKI_CURATOR_LLM_TIMEOUT_SEC,
    )
    return result.get("operations", [])


def compile_pending(
    conn=None,
    wiki_dir: Path | None = None,
    *,
    max_items: int | None = None,
) -> dict[str, Any]:
    """Compile one coherent pending topic batch into draft wiki pages."""
    from config.defaults import WIKI_COMPILER_BATCH_SIZE
    from src.db.connection import scoped_read, scoped_write
    from src.repositories import wiki_compiler_repo as repo
    from src.wiki.curator import _apply_operations
    from src.wiki.index_sync import build_catalog_text

    limit = max_items if max_items is not None else WIKI_COMPILER_BATCH_SIZE
    target_dir = Path(wiki_dir) if wiki_dir is not None else _WIKI_DIR

    if conn is not None:
        nuggets = repo.list_pending(conn, limit=limit)
    else:
        with scoped_read() as rconn:
            nuggets = repo.list_pending(rconn, limit=limit)

    empty = {
        "harvested": 0, "queued": 0, "skipped": 0,
        "created": 0, "updated": 0, "pages": [],
    }
    if not nuggets:
        return empty

    queue_ids = [item["queue_id"] for item in nuggets]
    entry_ids = [item["entry_id"] for item in nuggets]

    def _mark_compiling(wconn) -> None:
        repo.mark_compiling(wconn, queue_ids)

    if conn is not None:
        _mark_compiling(conn)
    else:
        with scoped_write() as wconn:
            _mark_compiling(wconn)

    catalog = build_catalog_text(target_dir, max_lines=350)
    try:
        operations = _call_compiler_llm(nuggets, catalog)
        created, updated, pages = _apply_operations(operations, target_dir)
    except Exception as exc:
        logger.warning("Wiki compiler batch failed: %s", exc, exc_info=True)

        error_text = str(exc)

        def _fail(wconn) -> None:
            repo.mark_batch_result(wconn, queue_ids, state="failed", error=error_text)

        if conn is not None:
            _fail(conn)
        else:
            with scoped_write() as wconn:
                _fail(wconn)
        return {**empty, "harvested": len(nuggets)}

    compiled_path = pages[0]["path"] if pages else None

    def _complete(wconn) -> None:
        if pages:
            repo.mark_batch_result(
                wconn, queue_ids, state="compiled", compiled_path=compiled_path,
            )
            repo.set_nugget_wiki_path(wconn, entry_ids, compiled_path)
        else:
            repo.mark_batch_result(
                wconn, queue_ids, state="failed", error="compiler returned no operations",
            )

    if conn is not None:
        _complete(conn)
    else:
        with scoped_write() as wconn:
            _complete(wconn)

    if pages:
        try:
            from src.wiki.indexer import invalidate_wiki_index
            invalidate_wiki_index()
        except Exception:
            logger.debug("Wiki index invalidate failed", exc_info=True)

    return {
        "harvested": len(nuggets),
        "queued": len(nuggets),
        "skipped": 0,
        "created": created,
        "updated": updated,
        "pages": pages,
    }


def run_nightly_wiki_compiler(conn=None, wiki_dir: Path | None = None) -> dict[str, Any]:
    """Enqueue unqueued nuggets, then compile one pending batch."""
    from src.db.connection import scoped_write

    if conn is not None:
        stats = enqueue_unqueued_nuggets(conn)
        result = compile_pending(conn, wiki_dir=wiki_dir)
    else:
        with scoped_write() as wconn:
            stats = enqueue_unqueued_nuggets(wconn)
        result = compile_pending(None, wiki_dir=wiki_dir)
    result["queued"] = stats["queued"]
    result["skipped"] = stats["skipped"]
    return result
