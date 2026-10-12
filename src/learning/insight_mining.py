"""Insight mining — scan activity_log for patterns, write insight/ wiki pages (WS-3)."""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from pathlib import Path

from config.settings import BASE_DIR
from src.db.connection import scoped_read

logger = logging.getLogger(__name__)
_WIKI_DIR = BASE_DIR / "wiki"


def mine_and_write_insights(wiki_dir: Path | None = None) -> int:
    """Mine activity_log for patterns, write up to 3 new insight wiki pages.

    Returns the count of pages written.
    """
    from src.llm.client import llm_call_structured
    from src.wiki.schema import (
        VALID_LINK_RELS,
        WikiLink,
        WikiPage,
        dedupe_wiki_links,
        render_page,
    )
    from src.wiki.write_guard import WikiWriteGuard, WikiValidationError

    wiki_path = wiki_dir or _WIKI_DIR
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")

    try:
        with scoped_read() as conn:
            rows = conn.execute(
                """SELECT a.project_id, p.market, a.action_type, a.summary, a.created_at
                   FROM activity_log a
                   JOIN projects p ON a.project_id = p.project_id
                   WHERE a.action_type IN (
                       'milestone_advanced', 'requires_review_set', 'operator_flag_set'
                   )
                     AND a.created_at >= datetime('now', '-90 days')
                   ORDER BY a.created_at DESC
                   LIMIT 200""",
            ).fetchall()
    except Exception:
        logger.debug("insight_mining: failed to query activity_log", exc_info=True)
        return 0

    if not rows:
        return 0

    activity_text = "\n".join(
        f"[{r['created_at'][:10] if r['created_at'] else 'unknown'}] "
        f"{r['project_id']} ({r['market']}): {r['summary']}"
        for r in rows
    )

    try:
        result = llm_call_structured(
            "insight_mining",
            [
                {"role": "system", "content": (
                    "You are analyzing FIX protocol onboarding activity logs to find cross-project patterns.\n\n"
                    "## Reasoning Protocol (MANDATORY)\n"
                    "You MUST reason in <think>...</think> tags BEFORE producing JSON:\n\n"
                    "<think>\n"
                    "Step 1: SCAN — Group the log entries by action_type and market. What clusters appear?\n"
                    "Step 2: QUANTIFY — For each cluster, how many projects are affected? (N out of total)\n"
                    "Step 3: PATTERN — Is there a recurring sequence, common blocker, or market-specific behavior?\n"
                    "Step 4: ACTIONABLE — Would knowing this pattern change how a PM approaches a new project? If no → skip\n"
                    "Step 5: NOVELTY — Is this obvious (e.g. 'projects advance milestones') or genuinely insightful?\n"
                    "Step 6: CONNECTIONS — What existing wiki pages does this relate to? (markets, concepts, entities)\n"
                    "</think>\n\n"
                    "Then output JSON.\n\n"
                    "Identify 1-3 significant patterns worth writing as wiki insight pages. "
                    "Return JSON with key 'insights': list of objects each with: "
                    "title (str), path (str like 'insight/hk_firewall'), body (2-3 sentences), "
                    "tags (list[str]), confidence ('low' or 'medium'), "
                    "links (list of {target: str, rel: str, note: str} — at least 2 links per insight "
                    "connecting to related markets, concepts, or patterns).\n"
                    "Valid rel values: depends_on, relates_to, operates_in, uses, derived_from, requires.\n"
                    "If no genuinely insightful pattern exists, return {\"insights\": []}."
                )},
                {"role": "user", "content": activity_text},
            ],
            {
                "type": "object",
                "properties": {
                    "insights": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "title": {"type": "string"},
                                "path": {"type": "string"},
                                "body": {"type": "string"},
                                "tags": {"type": "array", "items": {"type": "string"}},
                                "confidence": {"type": "string"},
                                "links": {
                                    "type": "array",
                                    "items": {
                                        "type": "object",
                                        "properties": {
                                            "target": {"type": "string"},
                                            "rel": {"type": "string"},
                                            "note": {"type": "string"},
                                        },
                                        "required": ["target", "rel"],
                                    },
                                },
                            },
                            "required": ["title", "path", "body", "links"],
                        },
                    }
                },
                "required": ["insights"],
            },
            temperature=0.3,
        )
    except Exception:
        logger.debug("insight_mining: LLM call failed", exc_info=True)
        return 0

    if not isinstance(result, dict):
        return 0

    guard = WikiWriteGuard()
    written = 0

    for insight in (result.get("insights") or [])[:3]:
        path_str = (insight.get("path") or "").strip("/")
        if not path_str or "/" not in path_str:
            continue
        category, stem = path_str.split("/", 1)
        out_path = wiki_path / category / f"{stem}.md"
        if out_path.exists():
            continue
        raw_links = insight.get("links") or []
        page_links = [
            WikiLink(target=lk["target"], rel=lk["rel"], note=lk.get("note", ""))
            for lk in raw_links
            if isinstance(lk, dict) and "target" in lk
            and lk.get("rel") in VALID_LINK_RELS
        ]
        from src.wiki.schema import ensure_bold_summary
        from src.wiki.index_sync import append_log, ensure_index_entry

        body = ensure_bold_summary(insight.get("body", "") or "", insight.get("title", "") or path_str)
        page = WikiPage(
            title=insight.get("title", ""),
            type="insight",
            tags=insight.get("tags") or [],
            sources=[f"system:insight-mining@{today}"],
            created=today,
            updated=today,
            confidence=insight.get("confidence", "low"),
            status="draft",
            body=body,
            links=dedupe_wiki_links(page_links),
        )
        try:
            guard.validate(
                page,
                path=path_str,
                require_summary=True,
                require_sources=True,
                check_type_dir=True,
            )
            out_path.parent.mkdir(parents=True, exist_ok=True)
            out_path.write_text(render_page(page), encoding="utf-8")
            ensure_index_entry(
                wiki_path, path_str, page.title or path_str, page_type="insight",
            )
            append_log(wiki_path, f"insight create {path_str}", writer="insight-mining")
            written += 1
        except WikiValidationError:
            logger.debug("insight_mining: validation failed for %s", path_str)
        except Exception:
            logger.debug("insight_mining: write failed for %s", path_str, exc_info=True)

    if written:
        from src.wiki.graph import invalidate_wiki_graph
        from src.wiki.indexer import invalidate_wiki_index
        invalidate_wiki_graph()
        invalidate_wiki_index()

    return written
