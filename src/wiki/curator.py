"""Wiki batch curator — harvest new DB data, call LLM to synthesize wiki pages."""
from __future__ import annotations

import json
import logging
import os
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import tiktoken

from config.settings import BASE_DIR, CONFIG
from src.wiki.schema import parse_page

logger = logging.getLogger(__name__)


def publication_v2_owns_writes() -> bool:
    """Return whether governed publication exclusively owns Wiki promotion."""
    from config import defaults
    return bool(defaults.WIKI_PUBLICATION_V2 and not defaults.WIKI_PUBLICATION_DISABLED)


_WIKI_DIR = BASE_DIR / "wiki"
_ENCODING = tiktoken.get_encoding("cl100k_base")


def _validate_wiki_path(rel_path: str) -> bool:
    """Return True only if rel_path is a safe relative path within the wiki directory."""
    normalized = os.path.normpath(rel_path)
    return (
        not normalized.startswith("..")
        and not os.path.isabs(normalized)
        and ".." not in normalized.split(os.sep)
    )


# ---------------------------------------------------------------------------
# Working-memory helpers for last-run tracking
# ---------------------------------------------------------------------------

def _ensure_system_project(conn) -> None:
    """Ensure the _system project row exists (needed for working_memory FK)."""
    conn.execute(
        """INSERT OR IGNORE INTO projects (project_id, project_status, ball_with, current_milestone)
           VALUES ('_system', 'Archived', 'Internal', 'M1_NetworkSetup')""",
    )


def _get_last_run(conn) -> str | None:
    """Read the last wiki curator run timestamp from working_memory."""
    from src.repositories.working_memory_repo import get_memory

    mem = get_memory(conn, "_system", "wiki_curator_last_run")
    if mem is None:
        return None
    return mem.get("memory_value")


def _set_last_run(conn, ts: str) -> None:
    """Persist the latest curator run timestamp."""
    from src.repositories.working_memory_repo import upsert_memory

    upsert_memory(conn, "_system", "wiki_curator_last_run", ts)


# ---------------------------------------------------------------------------
# Token estimation & index compression
# ---------------------------------------------------------------------------

def _estimate_tokens(text: str) -> int:
    """Estimate token count using cl100k_base encoding."""
    if not text:
        return 0
    return len(_ENCODING.encode(text))


def _compress_index(index_text: str, token_limit: int) -> str:
    """Compress wiki index to fit within token_limit.

    Strategy: strip descriptions, keep only path+title lines.
    If still over limit, keep only the last 100 entries.
    """
    if _estimate_tokens(index_text) <= token_limit:
        return index_text

    # Extract lines that look like index entries: "- [Title](path)"
    entries = re.findall(r"-\s+\[([^\]]+)\]\(([^)]+)\)", index_text)
    compressed_lines = ["# Wiki Index (compressed)\n"]
    for title, path in entries:
        compressed_lines.append(f"- [{title}]({path})")

    compressed = "\n".join(compressed_lines)
    if _estimate_tokens(compressed) <= token_limit:
        return compressed

    # Still over — keep only last 100 entries (most recent)
    truncated_entries = entries[-100:]
    truncated_lines = ["# Wiki Index (compressed, last 100 pages)\n"]
    for title, path in truncated_entries:
        truncated_lines.append(f"- [{title}]({path})")

    truncated = "\n".join(truncated_lines)
    if _estimate_tokens(truncated) <= token_limit:
        return truncated

    # Still over — binary-search for max entries that fit
    lo, hi = 1, len(truncated_entries)
    while lo < hi:
        mid = (lo + hi + 1) // 2
        candidate_entries = truncated_entries[-mid:]
        candidate_lines = ["# Wiki Index (compressed, truncated)\n"]
        for title, path in candidate_entries:
            candidate_lines.append(f"- [{title}]({path})")
        if _estimate_tokens("\n".join(candidate_lines)) <= token_limit:
            lo = mid
        else:
            hi = mid - 1

    final_entries = truncated_entries[-lo:]
    final_lines = ["# Wiki Index (compressed, truncated)\n"]
    for title, path in final_entries:
        final_lines.append(f"- [{title}]({path})")

    return "\n".join(final_lines)


def _build_curator_messages(
    system_prompt: str,
    index_text: str,
    schema_text: str,
    items: dict,
) -> list[dict[str, str]]:
    """Build the message list sent to the wiki_curator LLM."""
    user_parts = [
        "## Current Wiki Index\n" + index_text,
        "## Wiki Schema\n" + schema_text,
        "## New Items to Integrate\n" + json.dumps(items, indent=2, default=str),
        (
            "\nReturn your response as a JSON object with an 'operations' key "
            "containing an array of file operations."
        ),
    ]
    return [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": "\n\n".join(user_parts)},
    ]


def _estimate_curator_message_tokens(
    system_prompt: str,
    index_text: str,
    schema_text: str,
    items: dict,
) -> int:
    """Estimate prompt tokens using the same heuristic as src.llm.client."""
    from src.llm.client import _estimate_tokens as client_estimate

    return client_estimate(_build_curator_messages(system_prompt, index_text, schema_text, items))


def _items_from_tagged(tagged: list[tuple[str, dict]]) -> dict:
    batch: dict = {"evidence": [], "learned_rules": [], "feedback": []}
    for typ, item in tagged:
        batch[typ].append(item)
    return batch


def _split_into_batches(
    items: dict,
    system_prompt: str,
    index_text: str,
    schema_text: str,
    token_budget: int,
) -> list[dict]:
    """Split harvested items into batches that each fit within *token_budget*.

    Uses the LLM client's token estimator so batch sizes match MODEL_CONTEXT_WINDOW.
    Items are flattened, sorted by created_at, then greedily packed.
    """
    tagged: list[tuple[str, dict]] = []
    for item in items.get("evidence", []):
        tagged.append(("evidence", item))
    for item in items.get("learned_rules", []):
        tagged.append(("learned_rules", item))
    for item in items.get("feedback", []):
        tagged.append(("feedback", item))

    if not tagged:
        return []

    tagged.sort(key=lambda x: x[1].get("created_at", ""))

    empty_batch = {"evidence": [], "learned_rules": [], "feedback": []}
    base_tokens = _estimate_curator_message_tokens(system_prompt, index_text, schema_text, empty_batch)
    max_item_tokens = max(token_budget - base_tokens, 500) // 2

    for i, (typ, item) in enumerate(tagged):
        single = {"evidence": [], "learned_rules": [], "feedback": []}
        single[typ] = [item]
        if _estimate_curator_message_tokens(system_prompt, index_text, schema_text, single) > max_item_tokens:
            for field in ("content_text", "rule", "operator_note"):
                if field in item and len(item.get(field, "")) > 1000:
                    text = item[field]
                    lo, hi = 0, len(text)
                    while lo < hi:
                        mid = (lo + hi) // 2
                        trial_item = {**item, field: text[:mid]}
                        trial = {"evidence": [], "learned_rules": [], "feedback": []}
                        trial[typ] = [trial_item]
                        if _estimate_curator_message_tokens(system_prompt, index_text, schema_text, trial) <= max_item_tokens:
                            lo = mid + 1
                        else:
                            hi = mid
                    item = {**item, field: text[:max(lo - 1, 0)] + "... [truncated]"}
                    tagged[i] = (typ, item)
                    break

    batches: list[dict] = []
    current: list[tuple[str, dict]] = []

    for typ, item in tagged:
        candidate = current + [(typ, item)]
        candidate_items = _items_from_tagged(candidate)
        if current and _estimate_curator_message_tokens(
            system_prompt, index_text, schema_text, candidate_items,
        ) > token_budget:
            batches.append(_items_from_tagged(current))
            current = [(typ, item)]
        else:
            current = candidate

    if current:
        batches.append(_items_from_tagged(current))

    return batches


# ---------------------------------------------------------------------------
# Harvest
# ---------------------------------------------------------------------------

def _cap_harvested_items(items: dict, max_items: int) -> dict:
    """Keep the oldest *max_items* rows across all harvest sources."""
    tagged: list[tuple[str, dict]] = []
    for key in ("evidence", "learned_rules", "feedback"):
        for item in items.get(key, []):
            tagged.append((key, item))
    if len(tagged) <= max_items:
        return items

    tagged.sort(key=lambda pair: pair[1].get("created_at", ""))
    capped = {"evidence": [], "learned_rules": [], "feedback": []}
    for key, item in tagged[:max_items]:
        capped[key].append(item)
    return capped


def harvest_new_items(conn, since: str | None, *, max_items: int | None = None) -> dict:
    """Collect legacy curator inputs only when v2 does not own publication."""
    if publication_v2_owns_writes():
        return {"evidence": [], "learned_rules": [], "feedback": []}
    evidence: list[dict] = []
    learned_rules: list[dict] = []
    feedback: list[dict] = []

    # Evidence
    if since:
        rows = conn.execute(
            "SELECT * FROM evidence_store WHERE created_at > ? ORDER BY created_at",
            (since,),
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT * FROM evidence_store ORDER BY created_at DESC LIMIT 50",
        ).fetchall()
    evidence = [dict(r) for r in rows]

    # Legacy learned-rule publication markers cease to be authoritative once retired.
    from src.learning.legacy_retirement import written_to_kb_authoritative
    if written_to_kb_authoritative(conn):
        rows = conn.execute(
            """SELECT * FROM learned_rules
               WHERE status = 'approved' AND (written_to_kb = 0 OR written_to_kb IS NULL)""",
        ).fetchall()
        learned_rules = [dict(r) for r in rows]

    # AI feedback with operator notes
    if since:
        rows = conn.execute(
            """SELECT * FROM ai_feedback
               WHERE created_at > ? AND operator_note IS NOT NULL AND operator_note != ''
               ORDER BY created_at""",
            (since,),
        ).fetchall()
    else:
        rows = conn.execute(
            """SELECT * FROM ai_feedback
               WHERE operator_note IS NOT NULL AND operator_note != ''
               ORDER BY created_at DESC LIMIT 20""",
        ).fetchall()
    feedback = [dict(r) for r in rows]

    items = {"evidence": evidence, "learned_rules": learned_rules, "feedback": feedback}
    if max_items is not None and max_items > 0:
        items = _cap_harvested_items(items, max_items)
    return items


# ---------------------------------------------------------------------------
# LLM call
# ---------------------------------------------------------------------------

def _call_curator_llm(index_text: str, schema_text: str, items: dict) -> list:
    """Call LLM to synthesise wiki pages from harvested items.

    Returns a list of file-operation dicts.
    """
    from config.defaults import WIKI_CURATOR_LLM_TIMEOUT_SEC, WIKI_CURATOR_OUTPUT_RESERVE
    from src.llm.client import _trim_messages_to_fit, llm_call_structured

    prompt_path = BASE_DIR / "prompts" / "wiki_curator.md"
    system_prompt = (
        prompt_path.read_text(encoding="utf-8")
        if prompt_path.exists()
        else "You are a wiki curator."
    )

    schema: dict[str, Any] = {
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
        _build_curator_messages(system_prompt, index_text, schema_text, items),
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


# ---------------------------------------------------------------------------
# Process + write
# ---------------------------------------------------------------------------

def _load_curator_schema_text(wiki_dir: Path) -> str:
    guide_path = wiki_dir / "WIKI_GUIDE.md"
    schema_path = wiki_dir / "schema.md"
    if guide_path.exists():
        schema_text = guide_path.read_text(encoding="utf-8")
        if schema_path.exists():
            schema_text = schema_text + "\n\n" + schema_path.read_text(encoding="utf-8")
    else:
        schema_text = schema_path.read_text(encoding="utf-8") if schema_path.exists() else ""
    if len(schema_text) > 12000:
        schema_text = schema_text[:12000] + "\n…(truncated)"
    return schema_text


_HUB_OWNERS = {
    "b2badmin": "concept/internal-ops/b2badmin-account-lifecycle-reference",
    "settlement-instruction": "concept/settlement/settlement-instruction-and-dvp-reference",
    "taiwan-settlement": "concept/settlement/taiwan-counterparty-settlement-operations-reference",
}


def _is_forbidden_tip_create(path_no_ext: str, action: str) -> str | None:
    """Return rejection reason if create targets a hub-owned tip path."""
    if action != "create":
        return None
    p = path_no_ext.lower()
    if "b2badmin" in p and path_no_ext != _HUB_OWNERS["b2badmin"]:
        return f"B2BAdmin lifecycle facts must update {_HUB_OWNERS['b2badmin']} or a linked playbook"
    stem = p.rsplit("/", 1)[-1]
    if p.startswith("concept/settlement/") and "taiwan" in stem:
        if path_no_ext != _HUB_OWNERS["taiwan-settlement"]:
            return f"Taiwan settlement facts must update {_HUB_OWNERS['taiwan-settlement']} or a linked leaf"
    if p.startswith("concept/settlement/") and any(token in stem for token in ("settlement-instruction", "-ssi-", "-dvp-")):
        if path_no_ext != _HUB_OWNERS["settlement-instruction"]:
            return f"Settlement-instruction facts must update {_HUB_OWNERS['settlement-instruction']} or a linked leaf"
    # DTC one-liners: must enrich the hub table instead
    if p.endswith("-dtc-custodian-information") or p.endswith("_dtc_custodian_information"):
        return (
            "DTC codes must update concept/settlement/dtc-identifier-for-settlement-instructions "
            "(hub table row), not create a one-line DTC page"
        )
    if "/dtc-" in p and "dtc-identifier" not in p and action == "create":
        if "custodian" in p:
            return (
                "DTC custodian facts go on concept/settlement/dtc-identifier-for-settlement-instructions"
            )
    # New HK IPO tip concepts under b2b/settlement (except known hubs / durable pages)
    stem = p.rsplit("/", 1)[-1]
    if stem.startswith("hk-ipo-") or stem.startswith("hk_ipo_"):
        allowed_create = {
            "concept/hk_ipo_operator_reference",
        }
        if path_no_ext not in allowed_create and action == "create":
            # Allow create only if file truly new AND not a tip path under concept/
            if path_no_ext.startswith("concept/"):
                return (
                    "HK IPO facts must update pages linked from concept/hk_ipo_operator_reference "
                    f"(refused create of {path_no_ext})"
                )
    return None


def _apply_operations(operations: list, wiki_dir: Path) -> tuple[int, int, list[dict]]:
    """Validate legacy curator operations when v2 does not own Wiki writes."""
    if publication_v2_owns_writes():
        return 0, 0, []
    from src.wiki.schema import ensure_bold_summary, render_page
    from src.wiki.write_guard import WikiWriteGuard, WikiValidationError

    from src.wiki.write_authority import WRITER_COMPILER, authorize_wiki_write

    blocked = authorize_wiki_write(WRITER_COMPILER)
    if blocked:
        logger.warning("Wiki curator: write blocked (%s)", blocked)
        return 0, 0, []

    created = 0
    updated = 0
    pages: list[dict] = []

    for op in operations or []:
        action = op.get("action")
        rel_path = (op.get("path") or "").strip().removesuffix(".md")
        content = op.get("content", "")

        if action not in ("create", "update"):
            logger.warning("Wiki curator: unknown action '%s', skipping", action)
            continue
        if not rel_path or not content:
            logger.warning("Wiki curator: empty path or content, skipping")
            continue

        if not _validate_wiki_path(rel_path) and not _validate_wiki_path(rel_path + ".md"):
            logger.warning("Wiki curator rejected unsafe path: %s", rel_path)
            continue

        for legacy, canon in (
            ("entities/", "entity/"),
            ("concepts/", "concept/"),
            ("patterns/", "pattern/"),
        ):
            if rel_path.startswith(legacy):
                rel_path = canon + rel_path[len(legacy):]

        forbid = _is_forbidden_tip_create(rel_path, action)
        if forbid:
            logger.warning("Wiki curator: %s", forbid)
            continue

        page = parse_page(content)
        from src.wiki.schema import dedupe_wiki_links
        page.links = dedupe_wiki_links(page.links)
        path_no_ext = rel_path.removesuffix(".md")
        if not page.sources:
            page.sources = ["curator_harvest"]
        page.body = ensure_bold_summary(page.body or "", page.title or path_no_ext)
        content = render_page(page)

        guard = WikiWriteGuard()
        try:
            guard.validate(
                page,
                path=path_no_ext,
                require_summary=True,
                require_sources=True,
                check_type_dir=True,
            )
        except WikiValidationError as e:
            logger.warning("Wiki curator: skipping invalid page: %s", e)
            continue

        if not CONFIG.wiki_curator_auto_publish:
            content = re.sub(r'(?m)^status:\s*\S+', 'status: draft', content)
            page = parse_page(content)

        target = wiki_dir / f"{path_no_ext}.md"
        target.parent.mkdir(parents=True, exist_ok=True)

        if action == "create" and target.exists():
            action = "update"
            logger.info("Wiki curator: create→update for existing %s", path_no_ext)

        target.write_text(content, encoding="utf-8")
        if action == "create":
            created += 1
            pages.append({"path": path_no_ext, "title": page.title, "action": "created"})
            logger.info("Wiki curator: created %s", path_no_ext)
        else:
            updated += 1
            pages.append({"path": path_no_ext, "title": page.title, "action": "updated"})
            logger.info("Wiki curator: updated %s", path_no_ext)

    return created, updated, pages


def process_harvest_batch(items: dict, wiki_dir: Path) -> tuple[int, int, list[dict]]:
    """Parse LLM response, validate pages, write files.

    Calls ``_call_curator_llm`` internally with the current wiki index and schema.

    Returns ``(created, updated, pages)`` where *pages* is a list of dicts with
    keys ``path``, ``title``, and ``action`` ("created" or "updated").
    """
    from src.wiki.index_sync import append_log, build_catalog_text, rebuild_index

    index_text = build_catalog_text(wiki_dir, max_lines=350)
    schema_text = _load_curator_schema_text(wiki_dir)
    operations = _call_curator_llm(index_text, schema_text, items)
    created, updated, pages = _apply_operations(operations, wiki_dir)

    if pages:
        rebuild_index(wiki_dir)
        append_log(
            wiki_dir,
            f"curator batch: created={created} updated={updated}",
            writer="curator",
        )

    return created, updated, pages


# ---------------------------------------------------------------------------
# Orchestrator
# ---------------------------------------------------------------------------

def _persist_batch_updates(conn, batch: dict, now_str: str) -> None:
    """Apply per-batch DB bookkeeping (watermark + learned_rules flags)."""
    from src.learning.legacy_retirement import require_legacy_mutation
    batch_timestamps = []
    for key in ("evidence", "learned_rules", "feedback"):
        for item in batch.get(key, []):
            ts = item.get("created_at")
            if ts:
                batch_timestamps.append(ts)
    if batch_timestamps:
        _set_last_run(conn, max(batch_timestamps))
    learned_rules = batch.get("learned_rules", [])
    if learned_rules:
        require_legacy_mutation(conn, "learned_rules")
    for rule in learned_rules:
        rule_id = rule.get("rule_id")
        if rule_id:
            conn.execute(
                "UPDATE learned_rules SET written_to_kb = 1, kb_written_at = ? WHERE rule_id = ?",
                (now_str, rule_id),
            )


def _harvest_pending_learning_items(conn, max_items: int | None) -> dict:
    """Select sources needing capture or a matching extraction job for staged rollout."""
    from src.learning.governance_flags import outbox_v2_enabled
    tagged: list[tuple[str, dict]] = []
    queries = {
        "evidence": ("evidence_store", "evidence_id"),
        "learned_rules": ("learned_rules", "rule_id"),
        "feedback": ("ai_feedback", "feedback_id"),
    }
    for category, (table, id_col) in queries.items():
        rows = conn.execute(
            f"""SELECT src.* FROM {table} src
                LEFT JOIN learning_observations obs
                  ON obs.source_id = 'wiki-extraction:' || ? || ':' || src.{id_col}
                LEFT JOIN learning_outbox job
                  ON job.observation_id = obs.observation_id
                 AND job.job_type='wiki_extraction'
                 AND job.extractor_version='wiki-extraction-v1'
                WHERE obs.observation_id IS NULL OR (? AND job.job_id IS NULL)
                ORDER BY src.created_at, src.{id_col}""",
            (category, int(outbox_v2_enabled())),
        ).fetchall()
        tagged.extend((category, dict(row)) for row in rows)
    tagged.sort(key=lambda pair: (str(pair[1].get("created_at") or ""), str(pair[1])))
    if max_items is not None:
        tagged = tagged[:max_items]
    return _items_from_tagged(tagged)


def enqueue_curator_extraction(conn=None, *, max_items_per_run: int | None = None) -> dict:
    """Capture unqueued curator inputs as evidence-bound jobs; never publish Wiki pages."""
    from config.defaults import WIKI_CURATOR_MAX_ITEMS_PER_RUN
    from src.learning.jobs.sources import enqueue_wiki_learning_items

    cap = WIKI_CURATOR_MAX_ITEMS_PER_RUN if max_items_per_run is None else max_items_per_run
    max_items = cap if cap > 0 else None
    if conn is not None:
        _ensure_system_project(conn)
        result = enqueue_wiki_learning_items(conn, _harvest_pending_learning_items(conn, max_items))
        if result["cursor"]:
            _set_last_run(conn, result["cursor"])
    else:
        from src.db.connection import scoped_read, scoped_write
        with scoped_read() as rconn:
            items = _harvest_pending_learning_items(rconn, max_items)
        with scoped_write() as wconn:
            _ensure_system_project(wconn)
            result = enqueue_wiki_learning_items(wconn, items)
            if result["cursor"]:
                _set_last_run(wconn, result["cursor"])
    return {"harvested": result["captured"], "queued": result["enqueued"], "created": 0, "updated": 0, "pages": []}


def run_curator(
    conn=None,
    wiki_dir: Path | None = None,
    *,
    max_items_per_run: int | None = None,
) -> dict:
    """Full curator cycle: harvest -> split batches -> LLM per batch -> write -> bookkeeping.

    When ``conn`` is None, uses short-lived ``scoped_read`` / ``scoped_write``
    connections so the global write lock is not held during LLM calls.

    *max_items_per_run* caps harvested rows per invocation (default
    ``WIKI_CURATOR_MAX_ITEMS_PER_RUN``). Pass ``0`` to process the full backlog.

    Returns ``{"harvested": int, "created": int, "updated": int, "pages": list[dict]}``.
    """
    if publication_v2_owns_writes():
        return {"harvested": 0, "created": 0, "updated": 0, "pages": [], "disabled_by": "wiki_publication_v2"}

    import time
    from config.defaults import (
        WIKI_CURATOR_INDEX_TOKEN_LIMIT,
        WIKI_CURATOR_MAX_ITEMS_PER_RUN,
        WIKI_CURATOR_MAX_JOB_SECONDS,
        WIKI_CURATOR_TOKEN_BUDGET,
    )

    if max_items_per_run is None:
        max_items_per_run = WIKI_CURATOR_MAX_ITEMS_PER_RUN
    harvest_cap = max_items_per_run if max_items_per_run > 0 else None

    if wiki_dir is None:
        wiki_dir = _WIKI_DIR

    # Canonical singular dirs (see wiki/WIKI_GUIDE.md) — never scaffold legacy plurals
    for subdir in (
        "entity", "markets", "concept", "pattern", "playbook",
        "insight", "sources", "raw",
    ):
        (wiki_dir / subdir).mkdir(parents=True, exist_ok=True)

    if conn is not None:
        _ensure_system_project(conn)
        since = _get_last_run(conn)
        items = harvest_new_items(conn, since, max_items=harvest_cap)
    else:
        from src.db.connection import scoped_read, scoped_write

        with scoped_write() as wconn:
            _ensure_system_project(wconn)
        with scoped_read() as rconn:
            since = _get_last_run(rconn)
            items = harvest_new_items(rconn, since, max_items=harvest_cap)

    total = len(items["evidence"]) + len(items["learned_rules"]) + len(items["feedback"])
    if total == 0:
        logger.info("Wiki curator: nothing to process")
        return {"harvested": 0, "created": 0, "updated": 0, "pages": []}

    # Prefer compact catalog (path | title | type) over prose-only index
    from src.wiki.index_sync import build_catalog_text
    index_text = build_catalog_text(wiki_dir, max_lines=350)
    index_text = _compress_index(index_text, WIKI_CURATOR_INDEX_TOKEN_LIMIT)

    guide_path = wiki_dir / "WIKI_GUIDE.md"
    schema_path = wiki_dir / "schema.md"
    if guide_path.exists():
        schema_text = guide_path.read_text(encoding="utf-8")
        # Keep schema field reference if short enough budget remains later
        if schema_path.exists():
            schema_text = schema_text + "\n\n---\n\n" + schema_path.read_text(encoding="utf-8")
    else:
        schema_text = schema_path.read_text(encoding="utf-8") if schema_path.exists() else ""
    # Hard cap schema/guide blob
    if _estimate_tokens(schema_text) > 2500:
        schema_text = schema_text[:8000] + "\n\n…(truncated)"

    prompt_path = BASE_DIR / "prompts" / "wiki_curator.md"
    system_prompt = prompt_path.read_text(encoding="utf-8") if prompt_path.exists() else "You are a wiki curator."
    empty_batch = {"evidence": [], "learned_rules": [], "feedback": []}
    base_tokens = _estimate_curator_message_tokens(system_prompt, index_text, schema_text, empty_batch)

    if base_tokens >= WIKI_CURATOR_TOKEN_BUDGET:
        raise ValueError(
            f"Wiki curator: fixed prompt uses {base_tokens} tokens, "
            f"exceeds budget {WIKI_CURATOR_TOKEN_BUDGET}."
        )

    batches = _split_into_batches(items, system_prompt, index_text, schema_text, WIKI_CURATOR_TOKEN_BUDGET)
    logger.info(
        "Wiki curator: %d items split into %d batches (budget=%d tokens, base=%d)",
        total, len(batches), WIKI_CURATOR_TOKEN_BUDGET, base_tokens,
    )

    t0 = time.monotonic()
    all_created = 0
    all_updated = 0
    all_pages: list[dict] = []

    for batch_idx, batch in enumerate(batches):
        elapsed = time.monotonic() - t0
        if elapsed > WIKI_CURATOR_MAX_JOB_SECONDS:
            logger.warning("Wiki curator: hit %ds timeout after %d/%d batches", int(elapsed), batch_idx, len(batches))
            break

        operations = None
        for attempt in range(2):
            try:
                operations = _call_curator_llm(index_text, schema_text, batch)
                break
            except Exception as exc:
                from src.llm.watchdog import LLMTimeoutError

                is_timeout = isinstance(exc, LLMTimeoutError) or "timed out" in str(exc).lower()
                if is_timeout and attempt == 0:
                    logger.warning(
                        "Wiki curator: batch %d/%d timed out, retrying once",
                        batch_idx + 1, len(batches),
                    )
                    continue
                logger.error("Wiki curator: batch %d/%d failed: %s", batch_idx + 1, len(batches), exc)
                break
        if operations is None:
            break

        c, u, pages = _apply_operations(operations, wiki_dir)
        all_created += c
        all_updated += u
        all_pages.extend(pages)

        now_str = datetime.now(timezone.utc).isoformat()
        if conn is not None:
            _persist_batch_updates(conn, batch, now_str)
            conn.commit()
        else:
            from src.db.connection import scoped_write

            with scoped_write() as wconn:
                _persist_batch_updates(wconn, batch, now_str)

        # Refresh catalog for next batch
        if batch_idx < len(batches) - 1:
            from src.wiki.index_sync import rebuild_index
            rebuild_index(wiki_dir)
            index_text = build_catalog_text(wiki_dir, max_lines=350)
            index_text = _compress_index(index_text, WIKI_CURATOR_INDEX_TOKEN_LIMIT)

    # Final index rebuild and cache invalidation
    from src.wiki.index_sync import rebuild_index
    rebuild_index(wiki_dir)
    from src.wiki.indexer import invalidate_wiki_index
    from src.wiki.domain_index import invalidate_wiki_domain_index
    invalidate_wiki_index()
    invalidate_wiki_domain_index()

    # Append to log
    log_path = wiki_dir / "log.md"
    if not log_path.exists():
        log_path.write_text("# Wiki Log\n\n", encoding="utf-8")
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    entry = f"## [{today}] curator | {total} items -> {all_created} created, {all_updated} updated\n\n"
    log_path.write_text(
        log_path.read_text(encoding="utf-8") + entry, encoding="utf-8",
    )

    return {"harvested": total, "created": all_created, "updated": all_updated, "pages": all_pages}
