"""Email Knowledge Miner — extracts reusable domain knowledge from email threads via LLM."""
from __future__ import annotations

import json
import logging
import re
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from config.settings import BASE_DIR, CONFIG
from src.llm.client import llm_call_structured
from src.wiki.write_guard import WikiWriteGuard, WikiValidationError

logger = logging.getLogger(__name__)

_PROMPT_PATH = BASE_DIR / "prompts" / "email_knowledge_mining.md"
_MAX_THREADS_PER_RUN = 20

# Wiki subdirectory mapping by knowledge_type
_WIKI_DIR_MAP = {
    "domain_fact": "concept",
    "problem_solution": "pattern",
    "process_step": "playbook",
}

# Valid domain values for the knowledge taxonomy
_VALID_DOMAINS = {"b2b", "fix-connectivity", "conformance", "settlement", "internal-ops", "general"}

# Mapping from support_ticket category to domain
_TICKET_DOMAIN_MAP = {
    "fix_session": "fix-connectivity",
    "network": "fix-connectivity",
    "cert": "fix-connectivity",
    "config": "fix-connectivity",
    "trade": "settlement",
    "post_trade": "settlement",
    "access": "internal-ops",
    "monitoring": "internal-ops",
    "outreach": "b2b",
    "other": "general",
}

# LLM structured output schema for nuggets
_NUGGET_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "nuggets": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "knowledge_type": {
                        "type": "string",
                        "enum": ["domain_fact", "problem_solution", "process_step"],
                    },
                    "title": {"type": "string"},
                    "content": {"type": "string"},
                    "confidence": {
                        "type": "string",
                        "enum": ["high", "medium", "low"],
                    },
                    "is_internal": {"type": "boolean"},
                    "tags": {"type": "array", "items": {"type": "string"}},
                    "domain": {
                        "type": "string",
                        "enum": ["b2b", "fix-connectivity", "conformance", "settlement", "internal-ops", "general"],
                        "description": (
                            "Domain: b2b=B2B client onboarding/routing, "
                            "fix-connectivity=FIX sessions/VPN/certs, "
                            "conformance=UAT/test cases, settlement=post-trade, "
                            "internal-ops=internal IT tools/access, general=other"
                        ),
                    },
                    "milestone": {
                        "type": "string",
                        "description": "Optional milestone e.g. M5_ConformanceTestCases",
                    },
                    "symptoms": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Observable symptoms for wiki frontmatter tags",
                    },
                },
                "required": [
                    "knowledge_type",
                    "title",
                    "content",
                    "confidence",
                    "is_internal",
                    "domain",
                ],
            },
        },
    },
    "required": ["nuggets"],
}


@dataclass
class MiningReport:
    """Result summary of a single mining run."""

    run_id: str = ""
    started_at: str = ""
    threads_processed: int = 0
    entries_created: int = 0
    wiki_pages_written: int = 0
    errors: list[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _gen_id(prefix: str) -> str:
    """Generate a short unique ID like ``ekl_xxxx`` or ``emr_xxxx``."""
    return f"{prefix}{uuid.uuid4().hex[:12]}"


def _is_internal_sender(sender_email: str) -> bool:
    """Check if the sender domain is internal via wiki domain index or CONFIG."""
    if not sender_email or "@" not in sender_email:
        return False
    domain = sender_email.rsplit("@", 1)[1].lower()
    try:
        from src.wiki.domain_index import get_wiki_domain_index

        idx = get_wiki_domain_index()
        if idx.is_internal(domain):
            return True
    except Exception:
        pass
    company_domains = getattr(CONFIG, "company_domains", set())
    return domain in {d.lower() for d in company_domains}


def _group_by_thread(
    rows: list[dict[str, Any]],
) -> dict[str, list[dict[str, Any]]]:
    """Group email rows by thread_id, fallback thread_fp, fallback email_id."""
    groups: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        key = row.get("thread_id") or row.get("thread_fp") or row.get("email_id", "")
        if not key:
            continue
        groups.setdefault(key, []).append(row)
    return groups


def _build_transcript(emails: list[dict[str, Any]]) -> str:
    """Build a readable transcript from email rows sorted by processed_at."""
    sorted_emails = sorted(emails, key=lambda e: e.get("processed_at", ""))
    parts: list[str] = []
    for em in sorted_emails:
        direction = em.get("direction", "unknown")
        sender = em.get("sender_email", "unknown")
        subject = em.get("subject", "(no subject)")
        body = em.get("body_snippet") or ""
        parts.append(
            f"[{direction}] From: {sender}\n"
            f"Subject: {subject}\n"
            f"{body}\n"
        )
    return "\n---\n".join(parts)


def _load_prompt() -> str:
    """Read the mining prompt from prompts/email_knowledge_mining.md."""
    return _PROMPT_PATH.read_text(encoding="utf-8")


def wiki_search(query: str, *, top_k: int = 5, project_id: str | None = None) -> str:
    """Compatibility wrapper for curator-scoped dedup retrieval."""
    from src.wiki.retrieval import (
        WikiRetrievalMode,
        WikiSearchRequest,
        search_wiki,
    )

    return search_wiki(WikiSearchRequest(
        query=query,
        mode=WikiRetrievalMode.CURATOR,
        project_id=project_id,
        top_k=top_k,
    )).formatted


def _slugify(text: str) -> str:
    """Convert a title to a filename-safe slug."""
    slug = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    return slug[:60] if slug else "untitled"


def _looks_like_hk_ipo(title: str, content: str) -> bool:
    text = f"{title} {content}".lower()
    keys = (
        "hk ipo", "hk-ipo", "hkipo", "fini", "bcan", "ipo loan",
        "allotment", "public offer", "subscription day", "financing ratio",
    )
    return any(k in text for k in keys)


def _find_enrich_target(
    wiki_dir: Path,
    title: str,
    content: str,
    *,
    prefer_paths: list[str] | None = None,
) -> Path | None:
    """Return an existing wiki page path to enrich, or None to allow create."""
    from src.wiki.schema import parse_page

    def _usable(path: Path) -> Path | None:
        if not path.is_file():
            return None
        try:
            if (parse_page(path.read_text(encoding="utf-8")).status or "").lower() == "archived":
                return None
        except Exception:
            return None
        return path

    # Prefer explicit hub / known targets first
    for rel in prefer_paths or []:
        found = _usable(wiki_dir / f"{rel.removesuffix('.md')}.md")
        if found is not None:
            return found

    # Exact slug match under type/domain folders (fast path — no full TF-IDF)
    title_slug = _slugify(title)
    for wiki_type in ("concept", "pattern", "playbook", "insight", "entity"):
        type_dir = wiki_dir / wiki_type
        if not type_dir.is_dir():
            continue
        direct = _usable(type_dir / f"{title_slug}.md")
        if direct is not None:
            return direct
        for sub in type_dir.iterdir():
            if sub.is_dir():
                found = _usable(sub / f"{title_slug}.md")
                if found is not None:
                    return found

    # Lightweight title overlap against page titles (avoid full TF-IDF rebuild cost)
    title_tokens = set(re.findall(r"[a-z0-9]{3,}", title.lower()))
    if not title_tokens:
        return None
    best: tuple[float, Path] | None = None
    for wiki_type in ("concept", "pattern", "playbook", "insight", "entity"):
        type_dir = wiki_dir / wiki_type
        if not type_dir.is_dir():
            continue
        candidates = list(type_dir.glob("*.md"))
        for sub in type_dir.iterdir():
            if sub.is_dir():
                candidates.extend(sub.glob("*.md"))
        for md in candidates:
            found = _usable(md)
            if found is None:
                continue
            try:
                page = parse_page(found.read_text(encoding="utf-8"))
            except Exception:
                continue
            other = set(re.findall(r"[a-z0-9]{3,}", (page.title or found.stem).lower()))
            if not other:
                continue
            j = len(title_tokens & other) / len(title_tokens | other)
            if j >= 0.55 and (best is None or j > best[0]):
                best = (j, found)
    return best[1] if best else None


def _write_or_enrich_wiki_page(
    *,
    wiki_dir: Path,
    knowledge_type: str,
    domain: str,
    title: str,
    content: str,
    confidence: str,
    tags: list[str],
    visibility: str,
    source: str,
    now_iso: str,
    guard: WikiWriteGuard,
) -> str | None:
    """Enrich an existing page or create a new one under the new wiki contract.

    Returns relative path (posix) on success, else None.

    Direct miner writes are blocked. Kept for compiler reuse / tests that
    call the helper with an explicit allowed writer later.
    """
    from src.wiki.write_authority import WRITER_MINER, authorize_wiki_write

    blocked = authorize_wiki_write(WRITER_MINER)
    if blocked:
        logger.debug("Skipping miner wiki write: %s", blocked)
        return None

    from config.settings import BASE_DIR
    from src.wiki.schema import WikiPage, ensure_bold_summary, parse_page, render_page
    from src.wiki.index_sync import append_log, ensure_index_entry

    wiki_type = _WIKI_DIR_MAP.get(knowledge_type, "concept")
    body = ensure_bold_summary(content or "", title)
    day = now_iso[:10] if now_iso else datetime.now(timezone.utc).strftime("%Y-%m-%d")
    # String compare only — Path.resolve()/samefile can hang on Windows temp paths.
    is_primary_wiki = str(Path(wiki_dir)) == str(BASE_DIR / "wiki")

    prefer: list[str] = []
    if _looks_like_hk_ipo(title, content):
        prefer.append("concept/hk_ipo_operator_reference")

    target = _find_enrich_target(wiki_dir, title, content, prefer_paths=prefer)

    # Block new HK IPO tip creates — force hub enrich when possible
    if target is None and _looks_like_hk_ipo(title, content):
        hub = wiki_dir / "concept" / "hk_ipo_operator_reference.md"
        if hub.is_file():
            target = hub
        else:
            logger.warning(
                "Skipping wiki write for HK IPO tip %r — hub missing and no enrich target",
                title,
            )
            return None

    if target is not None:
        try:
            page = parse_page(target.read_text(encoding="utf-8"))
        except Exception as exc:
            logger.warning("Cannot read enrich target %s: %s", target, exc)
            return None
        block = (
            f"\n\n## From email mining ({day})\n\n"
            f"**{title}**\n\n{body.lstrip('*').strip() if body.startswith('**') else body}\n"
        )
        # Keep first bold summary of primary page
        page.body = (page.body or "").rstrip() + "\n" + block
        if source and source not in (page.sources or []):
            page.sources = list(page.sources or []) + [source]
        for tag in tags:
            if tag and tag not in page.tags:
                page.tags.append(tag)
        page.updated = day
        rel_path = target.relative_to(wiki_dir).as_posix()
        path_no_ext = rel_path.removesuffix(".md")
        try:
            guard.validate(page, path=path_no_ext, require_summary=False)
        except WikiValidationError as exc:
            logger.warning("Wiki enrich validation failed for %s: %s", path_no_ext, exc)
            return None
        target.write_text(render_page(page), encoding="utf-8")
        try:
            ensure_index_entry(wiki_dir, path_no_ext, page.title or title, page_type=page.type)
            append_log(wiki_dir, f"email-mine enrich {path_no_ext}: {title}", writer="email-miner")
        except Exception:
            logger.debug("index/log after enrich failed", exc_info=True)
        if is_primary_wiki:
            try:
                from src.wiki.indexer import invalidate_wiki_index
                from src.wiki.graph import invalidate_wiki_graph
                invalidate_wiki_index()
                invalidate_wiki_graph()
            except Exception:
                logger.debug("invalidate after enrich failed", exc_info=True)
        return rel_path

    # Fresh create (non-HK-IPO or no hub)
    wiki_subdir = f"{wiki_type}/{domain}" if domain else wiki_type
    slug = _slugify(title)
    rel_path = f"{wiki_subdir}/{slug}.md"
    path_no_ext = rel_path.removesuffix(".md")
    # Refuse forbidden tip paths (align with wiki reform / curator hubs)
    low = path_no_ext.lower()
    if low.endswith("-dtc-custodian-information") or low.endswith("_dtc_custodian_information"):
        logger.warning("email-miner refused DTC tip create %s", path_no_ext)
        return None
    stem = low.rsplit("/", 1)[-1]
    if stem.startswith("hk-ipo-") or stem.startswith("hk_ipo_"):
        if path_no_ext != "concept/hk_ipo_operator_reference":
            logger.warning("email-miner refused HK IPO tip create %s", path_no_ext)
            return None

    page = WikiPage(
        title=title,
        type=wiki_type,
        tags=tags,
        domains=[domain] if domain else [],
        sources=[source] if source else ["email_mining"],
        created=day,
        updated=day,
        confidence=confidence,
        status="draft",
        visibility=visibility,
        body=body,
    )
    try:
        guard.validate(
            page,
            path=path_no_ext,
            require_summary=True,
            require_sources=True,
            check_type_dir=True,
        )
    except WikiValidationError as exc:
        logger.warning("Wiki create validation failed for %s: %s", path_no_ext, exc)
        return None

    full_path = wiki_dir / rel_path
    full_path.parent.mkdir(parents=True, exist_ok=True)
    full_path.write_text(render_page(page), encoding="utf-8")
    try:
        ensure_index_entry(wiki_dir, path_no_ext, title, page_type=wiki_type)
        append_log(wiki_dir, f"email-mine create {path_no_ext}: {title}", writer="email-miner")
    except Exception:
        logger.debug("index/log after create failed", exc_info=True)
    if is_primary_wiki:
        try:
            from src.wiki.indexer import invalidate_wiki_index
            from src.wiki.graph import invalidate_wiki_graph
            invalidate_wiki_index()
            invalidate_wiki_graph()
        except Exception:
            logger.debug("invalidate after create failed", exc_info=True)
    return rel_path


def _is_duplicate_entry(conn: Any, knowledge_type: str, title: str) -> bool:
    """Return True if an entry with the same type+title already exists (case-insensitive)."""
    row = conn.execute(
        "SELECT 1 FROM email_knowledge_log "
        "WHERE knowledge_type = ? AND lower(title) = ? LIMIT 1",
        (knowledge_type, title.lower()),
    ).fetchone()
    return bool(row)


def _resolve_domain(nugget_domain: str | None, ticket_id: str | None, conn: Any) -> str:
    """Resolve domain: LLM suggestion > ticket category > 'general'."""
    if nugget_domain and nugget_domain in _VALID_DOMAINS and nugget_domain != "general":
        return nugget_domain
    if ticket_id:
        row = conn.execute(
            "SELECT category FROM support_tickets WHERE ticket_id=?", (ticket_id,)
        ).fetchone()
        if row:
            return _TICKET_DOMAIN_MAP.get(row["category"], "general")
    return "general"


def _build_existing_knowledge_context(conn: Any) -> str:
    """Return compact list of already-mined knowledge titles for the LLM dedup prompt."""
    rows = conn.execute(
        "SELECT knowledge_type, title FROM email_knowledge_log "
        "ORDER BY created_at DESC LIMIT 50"
    ).fetchall()
    if not rows:
        return ""
    lines = [f"- [{r[0]}] {r[1]}" for r in rows]
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Per-thread processing
# ---------------------------------------------------------------------------


def _with_read_conn(conn: Any | None, fn):
    """Run *fn(read_conn)* using *conn* or a short-lived ``scoped_read()``."""
    if conn is not None:
        return fn(conn)
    from src.db.connection import scoped_read

    with scoped_read() as rconn:
        return fn(rconn)


def _with_write_conn(conn: Any | None, fn):
    """Run *fn(write_conn)* using *conn* or a short-lived ``scoped_write()``."""
    if conn is not None:
        return fn(conn)
    from src.db.connection import scoped_write

    with scoped_write() as wconn:
        return fn(wconn)


def _process_thread(
    conn: Any | None,
    thread_id: str,
    emails: list[dict[str, Any]],
    run_id: str,
    wiki_dir: Path | None,
    report: MiningReport,
) -> bool:
    """Process one thread and return whether it reached a terminal success."""
    transcript = _build_transcript(emails)
    if not transcript.strip():
        return True

    # Determine project context
    project_id = next(
        (e.get("project_id") for e in emails if e.get("project_id")), None
    )

    # Get existing wiki knowledge for dedup prompt. Curator mode may inspect
    # all live scopes, but still excludes archived/superseded/unresolved pages.
    existing_knowledge = ""
    try:
        existing_knowledge = wiki_search(
            transcript[:200], top_k=5, project_id=project_id,
        )
    except Exception:
        logger.debug("Wiki search for dedup failed", exc_info=True)

    # Also include recent email_knowledge_log entries so LLM skips already-captured knowledge
    existing_kb_context = _with_read_conn(conn, _build_existing_knowledge_context)

    # Build messages for LLM
    system_prompt = _load_prompt()
    user_content = f"## Email Thread (thread_id: {thread_id})\n\n{transcript}"
    if project_id:
        user_content += f"\n\n## Project ID: {project_id}"
    if existing_knowledge:
        user_content += f"\n\n## Already Known (wiki)\n{existing_knowledge}"
    if existing_kb_context:
        user_content += (
            f"\n\n## Already Captured Knowledge (do NOT regenerate these topics):\n"
            f"{existing_kb_context}"
        )

    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": user_content},
    ]

    try:
        result = llm_call_structured(
            "knowledge_mining",
            messages,
            _NUGGET_SCHEMA,
            temperature=0.2,
        )
    except Exception as exc:
        report.errors.append(f"LLM call failed for thread {thread_id}: {exc}")
        logger.warning("LLM call failed for thread %s: %s", thread_id, exc)
        return False

    nuggets = result.get("nuggets", [])
    if not nuggets:
        return True

    # Check if any sender is internal
    has_internal_sender = any(
        _is_internal_sender(e.get("sender_email", "")) for e in emails
    )

    source_email_ids = json.dumps(
        [e.get("email_id", "") for e in emails]
    )
    ticket_id = next(
        (e.get("ticket_id") for e in emails if e.get("ticket_id")), None
    )

    guard = WikiWriteGuard()
    now_iso = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    errors_before = len(report.errors)

    for nugget in nuggets:
        _persist_nugget(
            conn,
            nugget=nugget,
            thread_id=thread_id,
            project_id=project_id,
            ticket_id=ticket_id,
            run_id=run_id,
            now_iso=now_iso,
            source_email_ids=source_email_ids,
            has_internal_sender=has_internal_sender,
            wiki_dir=wiki_dir,
            guard=guard,
            report=report,
        )
    return len(report.errors) == errors_before


def _persist_nugget(
    conn: Any | None,
    *,
    nugget: dict[str, Any],
    thread_id: str,
    project_id: str | None,
    ticket_id: str | None,
    run_id: str,
    now_iso: str,
    source_email_ids: str,
    has_internal_sender: bool,
    wiki_dir: Path | None,
    guard: WikiWriteGuard,
    report: MiningReport,
) -> None:
    knowledge_type = nugget.get("knowledge_type", "domain_fact")
    title = nugget.get("title", "Untitled")
    content = nugget.get("content", "")
    confidence = nugget.get("confidence", "medium")
    is_internal = nugget.get("is_internal", False)
    tags = list(nugget.get("tags") or [])
    milestone = nugget.get("milestone") or ""
    if milestone:
        tags.append(f"milestone:{milestone}")
    for symptom in (nugget.get("symptoms") or [])[:5]:
        if symptom:
            tags.append(f"symptom:{symptom}")

    def _insert(wconn):
        if _is_duplicate_entry(wconn, knowledge_type, title):
            logger.debug("Skipping duplicate nugget: [%s] %s", knowledge_type, title)
            return None

        _ = (wiki_dir, guard)
        domain = _resolve_domain(nugget.get("domain"), ticket_id, wconn)
        visibility = "internal_only" if (is_internal or has_internal_sender) else "public"

        wiki_path: str | None = None
        # Miner is intake only. Wiki pages are written by the compiler or a human.

        entry_id = _gen_id("ekl_")
        try:
            wconn.execute(
                "INSERT INTO email_knowledge_log "
                "(entry_id, created_at, thread_id, project_id, ticket_id, "
                "knowledge_type, title, content, confidence, visibility, "
                "wiki_path, source_emails, mining_run_id, domain) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    entry_id,
                    now_iso,
                    thread_id,
                    project_id,
                    ticket_id,
                    knowledge_type,
                    title,
                    content,
                    confidence,
                    visibility,
                    wiki_path,
                    source_email_ids,
                    run_id,
                    domain,
                ),
            )
            report.entries_created += 1
            try:
                from src.wiki.compiler import enqueue_nugget_for_compile

                enqueue_nugget_for_compile(
                    wconn,
                    {
                        "entry_id": entry_id,
                        "title": title,
                        "content": content,
                        "confidence": confidence,
                        "domain": domain,
                        "knowledge_type": knowledge_type,
                    },
                )
            except Exception:
                logger.debug("Wiki compiler enqueue skipped for %s", entry_id, exc_info=True)
        except Exception as exc:
            report.errors.append(
                f"DB insert failed for '{title}': {exc}"
            )
            logger.warning("DB insert failed: %s", exc)
        return True

    _with_write_conn(conn, _insert)


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------


def mine_email_knowledge(
    conn: Any | None,
    wiki_dir: Path | str | None = None,
) -> MiningReport:
    """Mine knowledge from unmined email threads.

    Args:
        conn: SQLite connection, or None to use scoped read/write (no lock
            held during LLM calls).
        wiki_dir: Directory for wiki page output. If None, wiki pages are not written.

    Returns:
        MiningReport with run statistics.
    """
    run_id = _gen_id("emr_")
    now_iso = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    report = MiningReport(run_id=run_id, started_at=now_iso)

    if wiki_dir is not None:
        wiki_dir = Path(wiki_dir)

    def _fetch_unmined(rconn):
        cursor = rconn.execute(
            "SELECT email_id, project_id, processed_at, thread_fp, subject, "
            "sender_email, direction, thread_id, body_snippet, ticket_id "
            "FROM processed_emails "
            "WHERE knowledge_mined = 0 AND body_snippet IS NOT NULL "
            "ORDER BY processed_at ASC"
        )
        col_names = [desc[0] for desc in cursor.description]
        return [dict(zip(col_names, row)) for row in cursor.fetchall()]

    rows = _with_read_conn(conn, _fetch_unmined)

    if not rows:
        return report

    # Group by thread
    thread_groups = _group_by_thread(rows)

    # Cap at MAX_THREADS_PER_RUN
    thread_ids = list(thread_groups.keys())[:_MAX_THREADS_PER_RUN]

    for thread_id in thread_ids:
        emails = thread_groups[thread_id]
        email_ids = [e["email_id"] for e in emails]
        placeholders = ",".join("?" for _ in email_ids)

        def _claim(wconn):
            wconn.execute(
                f"""UPDATE processed_emails
                    SET knowledge_mining_status='processing',
                        knowledge_mining_attempts=knowledge_mining_attempts+1,
                        knowledge_mining_lease_owner=?,
                        knowledge_mining_lease_expires_at=datetime('now', '+15 minutes'),
                        knowledge_mining_error=NULL
                    WHERE email_id IN ({placeholders}) AND knowledge_mined=0
                      AND (knowledge_mining_status IN ('pending','failed') OR
                           (knowledge_mining_status='processing' AND
                            knowledge_mining_lease_expires_at < datetime('now')))""",
                (run_id, *email_ids),
            )
            if conn is not None:
                wconn.commit()

        _with_write_conn(conn, _claim)
        success = False
        error_text = ""
        try:
            success = _process_thread(conn, thread_id, emails, run_id, wiki_dir, report)
        except Exception as exc:
            error_text = str(exc)
            report.errors.append(f"Thread {thread_id} failed: {exc}")
            logger.warning("Thread %s processing failed: %s", thread_id, exc, exc_info=True)
        report.threads_processed += 1

        def _finish(wconn):
            if success:
                wconn.execute(
                    f"""UPDATE processed_emails
                        SET knowledge_mined=1, knowledge_mining_status='mined',
                            knowledge_mined_at=datetime('now'), knowledge_mining_error=NULL,
                            knowledge_mining_lease_owner=NULL,
                            knowledge_mining_lease_expires_at=NULL
                        WHERE email_id IN ({placeholders})
                          AND knowledge_mining_lease_owner=?""",
                    (*email_ids, run_id),
                )
            else:
                wconn.execute(
                    f"""UPDATE processed_emails
                        SET knowledge_mining_status='failed', knowledge_mining_error=?,
                            knowledge_mining_lease_owner=NULL,
                            knowledge_mining_lease_expires_at=NULL
                        WHERE email_id IN ({placeholders})
                          AND knowledge_mining_lease_owner=?""",
                    ((error_text or f"Thread {thread_id} mining failed")[:2000], *email_ids, run_id),
                )
            if conn is not None:
                wconn.commit()

        _with_write_conn(conn, _finish)

    # Invalidate wiki caches after writing pages
    if report.wiki_pages_written > 0:
        try:
            from src.wiki.indexer import invalidate_wiki_index
            invalidate_wiki_index()
        except Exception:
            pass
        try:
            from src.wiki.domain_index import invalidate_wiki_domain_index
            invalidate_wiki_domain_index()
        except Exception:
            pass

    logger.info(
        "Mining run %s complete: %d threads, %d entries, %d wiki pages, %d errors",
        run_id,
        report.threads_processed,
        report.entries_created,
        report.wiki_pages_written,
        len(report.errors),
    )
    return report
