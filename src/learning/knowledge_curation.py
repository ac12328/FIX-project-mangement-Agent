"""Weekly knowledge curation: sync usage and backfill domains safely."""
from __future__ import annotations

import logging
from pathlib import Path

from config.settings import BASE_DIR
from src.db.connection import scoped_write
from src.wiki.schema import parse_page, render_page
from src.wiki.indexer import invalidate_wiki_index
from src.llm.client import llm_call_structured

logger = logging.getLogger(__name__)

_VALID_DOMAINS = frozenset(
    {"b2b", "fix-connectivity", "conformance", "settlement", "internal-ops", "general"}
)
_WIKI_DIR = BASE_DIR / "wiki"

_DOMAIN_SCHEMA = {
    "type": "object",
    "properties": {
        "domain": {
            "type": "string",
            "enum": ["b2b", "fix-connectivity", "conformance", "settlement", "internal-ops", "general"],
        }
    },
    "required": ["domain"],
}


def _update_wiki_frontmatter(path: Path, **kwargs) -> bool:
    """Update wiki frontmatter and return whether the file was changed."""
    if not path.exists():
        return False
    page = parse_page(path.read_text(encoding="utf-8"))
    for k, v in kwargs.items():
        setattr(page, k, v)
    path.write_text(render_page(page), encoding="utf-8")
    return True


def _llm_suggest_domain(title: str, content: str) -> str | None:
    """Ask LLM to suggest a domain for a knowledge entry. Returns None on error."""
    try:
        prompt = (
            f"Classify this knowledge entry into one domain.\n"
            f"Title: {title}\nContent: {content[:500]}\n\n"
            f"Domains: b2b=B2B client onboarding/routing, "
            f"fix-connectivity=FIX sessions/VPN/certs, "
            f"conformance=UAT/test cases, settlement=post-trade, "
            f"internal-ops=internal IT tools/access, general=other"
        )
        result = llm_call_structured(
            "triage",
            [{"role": "user", "content": prompt}],
            _DOMAIN_SCHEMA,
        )
        domain = result.get("domain") if isinstance(result, dict) else None
        return domain if domain in _VALID_DOMAINS else None
    except Exception as exc:
        logger.warning("LLM domain suggestion failed: %s", exc)
        return None


def run_knowledge_curation() -> dict:
    """Run all 4 curation passes. Returns a summary dict."""
    promoted = 0
    archived = 0
    backfilled = 0
    wiki_changed = False

    with scoped_write() as conn:
        # Pass 0: sync usage_count from wiki_usage_log
        conn.execute(
            "UPDATE email_knowledge_log SET usage_count = ("
            "  SELECT COUNT(*) FROM wiki_usage_log"
            "  WHERE wiki_path = email_knowledge_log.wiki_path"
            ") WHERE wiki_path IS NOT NULL"
        )
        conn.commit()

        # Containment: age, usage, and model confidence are not factual review.
        # Keep draft/active lifecycle states unchanged until WikiPage v2 records
        # explicit verification and supersession decisions.

        # Pass 1: LLM backfill domain=general (max 20/run)
        rows = conn.execute(
            "SELECT entry_id, title, content FROM email_knowledge_log "
            "WHERE domain='general' LIMIT 20"
        ).fetchall()
        for row in rows:
            new_domain = _llm_suggest_domain(row["title"], row["content"])
            if new_domain and new_domain != "general":
                conn.execute(
                    "UPDATE email_knowledge_log SET domain=? WHERE entry_id=?",
                    (new_domain, row["entry_id"]),
                )
                backfilled += 1
        conn.commit()

    if wiki_changed:
        invalidate_wiki_index()
        try:
            from src.wiki.graph import invalidate_wiki_graph
            from src.wiki.domain_index import invalidate_wiki_domain_index

            invalidate_wiki_graph()
            invalidate_wiki_domain_index()
        except Exception:
            logger.debug("Failed to invalidate secondary wiki caches", exc_info=True)

    return {"promoted": promoted, "archived": archived, "backfilled": backfilled}
