"""Weekly wiki health: rebuild index, lint, corpus audit summary."""
from __future__ import annotations

import logging
import re
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path

from config.settings import BASE_DIR
from src.wiki.index_sync import append_log, list_wiki_pages, rebuild_index
from src.wiki.lint import (
    find_dead_links,
    find_missing_from_index,
    find_active_placeholders,
    find_missing_summaries,
    find_semantic_orphans,
    find_unverified_pages,
    find_weak_sources,
    run_lint,
)
from src.wiki.schema import parse_page

logger = logging.getLogger(__name__)

_WIKI_DIR = BASE_DIR / "wiki"
_AUDIT_OUT = BASE_DIR / "docs" / "scratch" / "wiki_corpus_audit.md"
_WEAK_OUT = BASE_DIR / "docs" / "scratch" / "wiki_weak_sources.md"

# Generic title tokens that cause markets/* false-positive Jaccard
_GENERIC_TITLE_TOKENS = frozenset({
    "market", "markets", "knowledge", "rules", "global", "manual",
})


def _token_set(text: str) -> set[str]:
    return set(re.findall(r"[a-z0-9]{3,}", (text or "").lower()))


def _is_market_false_positive(p1: str, p2: str, t1: str, t2: str) -> bool:
    """True when both are markets/* and only share generic 'Market Knowledge' tokens."""
    if not (p1.startswith("markets/") and p2.startswith("markets/")):
        return False
    s1, s2 = _token_set(t1), _token_set(t2)
    if not s1 or not s2:
        return True
    overlap = s1 & s2
    meaningful = overlap - _GENERIC_TITLE_TOKENS
    return len(meaningful) == 0


def run_corpus_audit(wiki_dir: Path | None = None) -> dict:
    """Write audit + weak-sources reports. Returns summary dict for scheduler."""
    wiki_dir = Path(wiki_dir) if wiki_dir else _WIKI_DIR
    all_pages = list_wiki_pages(wiki_dir, include_archived=True)
    pages = list_wiki_pages(wiki_dir, include_archived=False)
    by_type = Counter(p[2] for p in pages)
    archived_n = len(all_pages) - len(pages)
    missing_idx = find_missing_from_index(wiki_dir)
    missing_sum = find_missing_summaries(wiki_dir)
    weak = find_weak_sources(wiki_dir)
    # Only count weak sources on active pages. Normalize both sides once.
    active_paths = {p.removesuffix(".md").strip("/") for p, _, _ in pages}
    weak_active = [
        w for w in weak
        if w["file"].removesuffix(".md").strip("/") in active_paths
    ]
    placeholders = find_active_placeholders(wiki_dir)
    semantic_orphans = find_semantic_orphans(wiki_dir)
    orphan_clusters: Counter[str] = Counter()
    orphans_with_outbound = 0
    for item in semantic_orphans:
        path = item["file"].removesuffix(".md")
        parts = path.split("/")
        cluster = "/".join(parts[:2]) if len(parts) > 2 else f"{parts[0]}/root"
        orphan_clusters[cluster] += 1
        page = parse_page((wiki_dir / item["file"]).read_text(encoding="utf-8"))
        if page.links:
            orphans_with_outbound += 1

    lines = [
        "# Wiki Corpus Audit",
        "",
        f"Generated: {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}",
        "",
        f"Total content pages (incl. archived stubs): {len(all_pages)}",
        f"Active content pages: {len(pages)} (archived stubs: {archived_n})",
        f"Index coverage: {len(all_pages) - len(missing_idx)}/{len(all_pages)} "
        f"({100 * (1 - len(missing_idx) / max(len(all_pages), 1)):.1f}%)",
        f"Missing summaries: {len(missing_sum)}",
        f"Weak sources (active files): {len(weak_active)}",
        f"Active restricted placeholders: {len(placeholders)}",
        "",
        "## Counts by type (active only)",
    ]
    for t, n in sorted(by_type.items(), key=lambda x: -x[1]):
        lines.append(f"- {t}: {n}")

    lines += [
        "",
        "## Near-duplicate candidates "
        "(title Jaccard ≥ 0.55, same type, active only; markets false-positives filtered)",
    ]
    by_t: dict[str, list[tuple[str, str]]] = defaultdict(list)
    for path, title, ptype in pages:
        by_t[ptype].append((path, title))
    pairs = 0
    skipped_market_fp = 0
    for ptype, items in by_t.items():
        for i, (p1, t1) in enumerate(items):
            s1 = _token_set(t1)
            if len(s1) < 2:
                continue
            for p2, t2 in items[i + 1 :]:
                s2 = _token_set(t2)
                if not s2:
                    continue
                j = len(s1 & s2) / len(s1 | s2)
                if j < 0.55:
                    continue
                if _is_market_false_positive(p1, p2, t1, t2):
                    skipped_market_fp += 1
                    continue
                lines.append(f"- [{j:.2f}] `{p1}` ↔ `{p2}` — {t1} / {t2}")
                pairs += 1
                if pairs >= 40:
                    break
            if pairs >= 40:
                break
        if pairs >= 40:
            break
    if pairs == 0:
        lines.append("- (none found)")
    lines.append(f"\n_Markets false-positive pairs filtered: {skipped_market_fp}_")

    lines += [
        "",
        "## Semantic navigation debt (active only; report only)",
        f"- Total semantic orphans: {len(semantic_orphans)}",
        f"- Orphans with existing outbound typed links: {orphans_with_outbound}",
    ]
    for cluster, count in sorted(orphan_clusters.items(), key=lambda item: (-item[1], item[0])):
        lines.append(f"- {cluster}: {count}")

    lines += ["", "## Long-tail inventory (active only)"]
    domains = ("b2b", "settlement", "internal-ops")
    for domain in domains:
        domain_pages = [
            (path, title, ptype) for path, title, ptype in pages
            if f"/{domain}/" in f"/{path}/"
        ]
        thin = 0
        no_links = 0
        for path, _title, _ptype in domain_pages:
            page = parse_page((wiki_dir / f"{path}.md").read_text(encoding="utf-8"))
            if len((page.body or "").split()) <= 35:
                thin += 1
            if not page.links:
                no_links += 1
        lines.append(
            f"- {domain}: active={len(domain_pages)}, thin<=35w={thin}, no_typed_links={no_links}"
        )

    _AUDIT_OUT.parent.mkdir(parents=True, exist_ok=True)
    _AUDIT_OUT.write_text("\n".join(lines) + "\n", encoding="utf-8")

    # Operator checklist for weak sources
    weak_lines = [
        "# Wiki Weak Sources Checklist",
        "",
        f"Generated: {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}",
        "",
        "Pages with empty or placeholder-only sources. Prefer:",
        "`email:<id>`, `doc:<path>`, `operator:name@YYYY-MM-DD`, `rule:<id>`.",
        "",
        f"**Count:** {len(weak_active)}",
        "",
    ]
    for w in sorted(weak_active, key=lambda x: x["file"]):
        weak_lines.append(f"- `{w['file']}` — sources={w.get('sources', [])}")
    _WEAK_OUT.write_text("\n".join(weak_lines) + "\n", encoding="utf-8")

    return {
        "active": len(pages),
        "archived": archived_n,
        "missing_index": len(missing_idx),
        "missing_summaries": len(missing_sum),
        "weak_sources": len(weak_active),
        "active_placeholders": len(placeholders),
        "semantic_orphans": len(semantic_orphans),
        "orphan_clusters": dict(sorted(orphan_clusters.items())),
        "orphans_with_outbound": orphans_with_outbound,
        "near_dup_pairs": pairs,
        "market_fp_filtered": skipped_market_fp,
        "audit_path": str(_AUDIT_OUT),
        "weak_path": str(_WEAK_OUT),
    }


def run_weekly_health(wiki_dir: Path | None = None) -> dict:
    """Rebuild index, lint, audit. Returns combined summary for Telegram."""
    wiki_dir = Path(wiki_dir) if wiki_dir else _WIKI_DIR
    rebuild_index(wiki_dir)
    report = run_lint(wiki_dir)
    audit = run_corpus_audit(wiki_dir)
    dead = find_dead_links(wiki_dir)
    unverified = find_unverified_pages(wiki_dir)
    append_log(
        wiki_dir,
        (
            f"weekly-health: active={audit['active']} weak={audit['weak_sources']} "
            f"dead={len(dead)} orphans={audit['semantic_orphans']} near_dups={audit['near_dup_pairs']}"
        ),
        writer="weekly-health",
    )
    return {
        **audit,
        "dead_links": len(dead),
        "unverified": len(unverified),
        "orphans": audit["semantic_orphans"],
        "lint_report_chars": len(report),
    }
