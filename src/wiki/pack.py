"""Budget-aware packing of wiki hits for agent context / tool strings."""
from __future__ import annotations

from pathlib import Path
from typing import Any

from config.settings import BASE_DIR


def _read_page_body(path_no_ext: str, wiki_dir: Path | None = None) -> str:
    root = Path(wiki_dir) if wiki_dir else BASE_DIR / "wiki"
    md = root / f"{path_no_ext.removesuffix('.md')}.md"
    if not md.is_file():
        return ""
    try:
        from src.wiki.schema import parse_page
        page = parse_page(md.read_text(encoding="utf-8"))
        return (page.body or "").strip()
    except Exception:
        return ""


def attach_playbook_bodies(
    hits: list[dict[str, Any]],
    *,
    query: str = "",
    max_playbooks: int = 1,
    playbook_chars: int = 4000,
    wiki_dir: Path | None = None,
) -> list[dict[str, Any]]:
    """Attach fuller page body to top playbook hit(s) (read-after-search).

    Mutates a shallow copy of hits; sets ``full_body`` and upgrades ``content``
    for procedure answers so Steps are not truncated mid-playbook.
    """
    from src.wiki.rank import detect_query_intent

    intent = detect_query_intent(query)
    # Always allow attach for explicit playbook top hits; force if procedure intent
    attached = 0
    out: list[dict[str, Any]] = []
    for hit in hits:
        h = dict(hit)
        ptype = (h.get("page_type") or "").lower()
        path = h.get("path") or ""
        if h.get("full_body"):
            # Already attached (e.g. wiki_search_hits); keep
            out.append(h)
            attached += 1
            continue
        should = (
            attached < max_playbooks
            and ptype == "playbook"
            and (intent.procedure or attached == 0)
        )
        # Only auto-attach first playbook in list (usually highest rank)
        if should and attached == 0:
            body = _read_page_body(path, wiki_dir=wiki_dir)
            if body:
                if playbook_chars > 0 and len(body) > playbook_chars:
                    body = body[:playbook_chars] + "\n...[playbook truncated]"
                h["full_body"] = body
                h["content"] = body
                h["snippet"] = body[:800] + ("..." if len(body) > 800 else "")
                h["section"] = h.get("section") or "full"
                attached += 1
        out.append(h)
    return out


def pack_wiki_hits(
    hits: list[dict[str, Any]],
    *,
    budget: int = 6000,
    summary_budget_per_hit: int = 200,
    query: str = "",
    wiki_dir: Path | None = None,
) -> str:
    """Pack hits into a single string under *budget* characters.

    Strategy:
    1. Always emit header + summary for every hit (cheap).
    2. Spend remaining budget on rank-1 content (prefer full_body if present).
    3. Then remaining hits' snippets.
    """
    if not hits:
        return ""

    enriched = attach_playbook_bodies(hits, query=query, wiki_dir=wiki_dir)
    parts: list[str] = []
    used = 0

    # Pass 1: headers + summaries for all
    headers: list[str] = []
    for r in enriched:
        conf = r.get("confidence") or "?"
        status = r.get("status") or "?"
        path = r.get("path") or ""
        title = r.get("title") or path
        summary = (r.get("summary") or "").strip()
        if len(summary) > summary_budget_per_hit:
            summary = summary[:summary_budget_per_hit] + "..."
        header = (
            f"[wiki:{path} > {r.get('section', '')}] "
            f"(score={r.get('score')} conf={conf} status={status} type={r.get('page_type') or '?'})"
        )
        if r.get("trust_warning"):
            header = str(r["trust_warning"]) + "\n" + header
        block = header + "\n"
        if title:
            block += f"Title: {title}\n"
        if summary:
            block += f"**{summary}**\n"
        headers.append(block)
        used += len(block) + 5  # separator allowance

    remaining = max(0, budget - used)

    # Pass 2: attach bodies/snippets in rank order
    bodies: list[str] = []
    for i, r in enumerate(enriched):
        if remaining <= 80:
            bodies.append("")  # summary-only
            continue
        content = r.get("full_body") or r.get("content") or r.get("snippet") or ""
        # First hit gets more budget; others share rest
        if i == 0:
            allot = min(len(content), max(remaining // 2, min(remaining, 3500)))
        else:
            allot = min(len(content), max(200, remaining // max(1, len(enriched) - i)))
        chunk = content[:allot]
        if len(content) > allot:
            chunk += "..."
        # neighbors cheap
        nb_lines = ""
        for nb in (r.get("neighbors") or [])[:2]:
            nb_lines += (
                f"\n  -> [{nb.get('rel')}] {nb.get('path')} ({nb.get('title')})"
            )
        body = chunk + nb_lines
        bodies.append(body)
        remaining -= len(body)

    for h, b in zip(headers, bodies):
        parts.append(h + b if b else h.rstrip() + "\n")

    return "\n\n---\n\n".join(parts)
