# src/wiki/lint.py
"""Wiki lint — health-check for orphans, dead links, stale pages."""
from __future__ import annotations

import logging
import re
from datetime import datetime, timezone, timedelta
from pathlib import Path

from config.settings import BASE_DIR
from src.wiki.schema import (
    LEGACY_PLURAL_DIRS,
    has_bold_summary,
    parse_page,
    duplicate_wiki_link_keys,
    extract_wiki_links,
    path_type_dir_mismatch,
    sources_are_weak,
)

logger = logging.getLogger(__name__)

_WIKI_DIR = BASE_DIR / "wiki"
_EXCLUDE_DIRS = {"raw", ".obsidian"}
_STRUCTURAL_FILES = frozenset({
    "index.md", "README.md", "lint_report.md", "log.md", "schema.md",
    "graph-index.md", "WIKI_GUIDE.md",
})

# Root / structural pages — no inbound link required
_ORPHAN_EXEMPT = _STRUCTURAL_FILES
_RESTRICTED_PLACEHOLDER = re.compile(
    r"restricted project-specific material (?:was )?migrated to (?:approved )?evidence storage",
    re.IGNORECASE,
)


def _read_md_text(path: Path) -> str:
    """Read markdown with UTF-8 preferred; fall back for legacy encodings."""
    raw = path.read_bytes()
    for enc in ("utf-8", "utf-8-sig", "cp1252", "latin-1"):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", errors="replace")


def _iter_wiki_pages(wiki_dir: Path):
    """Yield (relative_path, content) for all wiki pages excluding raw/.

    Uses rglob to match WikiIndex — nested paths like playbook/fix-connectivity/*.md
    are included.
    """
    for subdir in sorted(wiki_dir.iterdir()):
        if not subdir.is_dir() or subdir.name in _EXCLUDE_DIRS:
            continue
        for md_path in sorted(subdir.rglob("*.md")):
            if md_path.name in _STRUCTURAL_FILES:
                continue
            rel = md_path.relative_to(wiki_dir).as_posix()
            yield rel, _read_md_text(md_path)


def find_dead_links(wiki_dir: Path) -> list[dict]:
    """Find [[wiki-links]] that point to non-existent pages."""
    existing_stems: set[str] = set()
    for rel, _ in _iter_wiki_pages(wiki_dir):
        existing_stems.add(Path(rel).stem)
        existing_stems.add(rel.removesuffix(".md"))

    dead: list[dict] = []
    for rel, content in _iter_wiki_pages(wiki_dir):
        for link in extract_wiki_links(content):
            if link not in existing_stems:
                dead.append({"source": rel, "target": link})
    return dead


def find_semantic_orphans(wiki_dir: Path) -> list[dict]:
    """Find active pages with no inbound typed or explicit wiki links."""
    pages: dict[str, object] = {}
    inbound: dict[str, int] = {}
    for rel, content in _iter_wiki_pages(wiki_dir):
        path = rel.removesuffix(".md")
        page = parse_page(content)
        pages[path] = page
        inbound.setdefault(path, 0)

    for source_path, page in pages.items():
        if (page.status or "active").lower() == "archived":
            continue
        targets = {link.target for link in page.links}
        targets.update(extract_wiki_links(page.body or ""))
        for target in targets:
            target_page = pages.get(target)
            if target_page is None or (target_page.status or "active").lower() == "archived":
                continue
            inbound[target] = inbound.get(target, 0) + 1

    return [
        {"file": f"{path}.md"}
        for path, page in pages.items()
        if (page.status or "active").lower() != "archived"
        and page.type not in {"market", "insight"}
        and inbound.get(path, 0) == 0
    ]


def find_orphan_pages(wiki_dir: Path) -> list[dict]:
    """Backward-compatible alias for semantic-orphan checks."""
    return find_semantic_orphans(wiki_dir)


def find_typed_link_issues(wiki_dir: Path) -> list[dict]:
    """Validate typed targets and active/archive link boundaries."""
    pages: dict[str, object] = {}
    for rel, content in _iter_wiki_pages(wiki_dir):
        pages[rel.removesuffix(".md")] = parse_page(content)
    issues: list[dict] = []
    for source, page in pages.items():
        for target, rel in duplicate_wiki_link_keys(page.links):
            issues.append({
                "source": f"{source}.md",
                "target": target,
                "rel": rel,
                "issue": "duplicate_edge",
            })
        for link in page.links:
            target = pages.get(link.target)
            if target is None:
                issues.append({"source": f"{source}.md", "target": link.target, "issue": "missing_target"})
            elif (
                (page.status or "active").lower() != "archived"
                and (target.status or "active").lower() == "archived"
                and link.rel != "supersedes"
            ):
                issues.append({"source": f"{source}.md", "target": link.target, "issue": "active_to_archived"})
    return issues


def find_archive_redirect_issues(wiki_dir: Path) -> list[dict]:
    """Return invalid archived canonical pointers, including cycles."""
    from src.wiki.corpus import list_canonical_pages
    from src.wiki.duplicates import audit_archive_redirects

    return [
        {"file": f"{check.path}.md", "target": check.target, "issues": list(check.issues)}
        for check in audit_archive_redirects(list_canonical_pages(wiki_dir))
        if check.status != "ok"
    ]


def find_stale_pages(wiki_dir: Path, stale_days: int = 90) -> list[dict]:
    """Find pages with updated date older than stale_days."""
    cutoff = datetime.now(timezone.utc) - timedelta(days=stale_days)
    stale: list[dict] = []
    for rel, content in _iter_wiki_pages(wiki_dir):
        page = parse_page(content)
        if not page.updated:
            continue
        try:
            updated_dt = datetime.strptime(page.updated, "%Y-%m-%d").replace(tzinfo=timezone.utc)
        except (ValueError, TypeError):
            continue
        if updated_dt < cutoff:
            stale.append({"file": rel, "updated": page.updated})
    return stale


_UNVERIFIED_RE = re.compile(
    r'#\s*TODO[:\s]|^\s*[-*]?\s*Verify\b|_Migrated from AgentFIXB',
    re.IGNORECASE | re.MULTILINE,
)


def find_unverified_pages(wiki_dir: Path) -> list[dict]:
    """Find pages containing TODO/verify/migration markers in their content."""
    unverified: list[dict] = []
    for rel, content in _iter_wiki_pages(wiki_dir):
        matching_lines = [line for line in content.splitlines() if _UNVERIFIED_RE.search(line)]
        if matching_lines:
            unverified.append({"file": rel, "marker_count": len(matching_lines)})
    return unverified


def find_missing_summaries(wiki_dir: Path) -> list[dict]:
    """Pages whose body lacks a bold one-sentence summary."""
    missing: list[dict] = []
    for rel, content in _iter_wiki_pages(wiki_dir):
        if rel in _ORPHAN_EXEMPT or "/" not in rel:
            continue
        page = parse_page(content)
        if not has_bold_summary(page.body or ""):
            missing.append({"file": rel})
    return missing


def find_missing_from_index(wiki_dir: Path) -> list[dict]:
    """Content pages not linked from index.md."""
    from src.wiki.index_sync import index_paths, list_wiki_pages

    indexed = index_paths(wiki_dir)
    missing: list[dict] = []
    for path_no_ext, title, _ptype in list_wiki_pages(wiki_dir):
        if path_no_ext not in indexed:
            missing.append({"file": f"{path_no_ext}.md", "title": title})
    return missing


def find_type_dir_mismatches(wiki_dir: Path) -> list[dict]:
    """Pages whose path top-level dir does not match frontmatter type."""
    bad: list[dict] = []
    for rel, content in _iter_wiki_pages(wiki_dir):
        if rel in _ORPHAN_EXEMPT or "/" not in rel:
            continue
        page = parse_page(content)
        if not page.type:
            continue
        path_no_ext = rel.removesuffix(".md")
        msg = path_type_dir_mismatch(path_no_ext, page.type)
        if msg:
            bad.append({"file": rel, "issue": msg})
    return bad


def find_weak_sources(wiki_dir: Path) -> list[dict]:
    """Pages with empty or placeholder-only sources."""
    weak: list[dict] = []
    for rel, content in _iter_wiki_pages(wiki_dir):
        if rel in _ORPHAN_EXEMPT or "/" not in rel:
            continue
        page = parse_page(content)
        if sources_are_weak(list(page.sources or [])):
            weak.append({"file": rel, "sources": list(page.sources or [])})
    return weak


def find_active_placeholders(wiki_dir: Path) -> list[dict]:
    """Find active pages whose reusable body is only a restricted migration notice."""
    found: list[dict] = []
    for rel, content in _iter_wiki_pages(wiki_dir):
        page = parse_page(content)
        if (page.status or "active").lower() == "archived":
            continue
        body = (page.body or "").strip()
        if _RESTRICTED_PLACEHOLDER.search(body):
            found.append({"file": rel, "status": page.status or "active"})
    return found


def find_legacy_plural_pages(wiki_dir: Path) -> list[dict]:
    """Any .md files under legacy plural directories."""
    found: list[dict] = []
    for name in sorted(LEGACY_PLURAL_DIRS):
        d = wiki_dir / name
        if not d.is_dir():
            continue
        for md in d.rglob("*.md"):
            rel = md.relative_to(wiki_dir).as_posix()
            found.append({"file": rel})
    return found


def run_lint(wiki_dir: Path | None = None, stale_days: int = 90) -> str:
    """Run all lint checks and write report. Returns report text."""
    if wiki_dir is None:
        wiki_dir = _WIKI_DIR

    dead = find_dead_links(wiki_dir)
    typed_link_issues = find_typed_link_issues(wiki_dir)
    archive_issues = find_archive_redirect_issues(wiki_dir)
    orphans = find_semantic_orphans(wiki_dir)
    stale = find_stale_pages(wiki_dir, stale_days=stale_days)
    unverified = find_unverified_pages(wiki_dir)
    no_summary = find_missing_summaries(wiki_dir)
    not_indexed = find_missing_from_index(wiki_dir)
    type_mismatch = find_type_dir_mismatches(wiki_dir)
    weak_src = find_weak_sources(wiki_dir)
    placeholders = find_active_placeholders(wiki_dir)
    legacy = find_legacy_plural_pages(wiki_dir)

    lines = ["# Wiki Lint Report", "",
             f"Generated: {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}", "",
             f"## Dead Links ({len(dead)}) — error"]
    for d in dead:
        lines.append(f"- `{d['source']}` → [[{d['target']}]]")
    lines += ["", f"## Typed link issues ({len(typed_link_issues)}) — error"]
    for item in typed_link_issues:
        relation = f" --[{item['rel']}]-->" if item.get("rel") else " →"
        lines.append(
            f"- `{item['source']}`{relation} `{item['target']}` — {item['issue']}"
        )
    lines += ["", f"## Archive redirect issues ({len(archive_issues)}) — error"]
    for item in archive_issues:
        lines.append(f"- `{item['file']}` → `{item.get('target')}` — {item['issues']}")
    lines += ["", f"## Missing from index.md ({len(not_indexed)}) — error after rebuild"]
    for m in not_indexed[:50]:
        lines.append(f"- `{m['file']}` — {m.get('title', '')}")
    if len(not_indexed) > 50:
        lines.append(f"- … and {len(not_indexed) - 50} more")
    lines += ["", f"## Legacy plural directory pages ({len(legacy)}) — error"]
    for g in legacy:
        lines.append(f"- `{g['file']}`")
    lines += ["", f"## Semantic orphan pages ({len(orphans)}) — warn"]
    for o in orphans:
        lines.append(f"- `{o['file']}`")
    lines += ["", f"## Stale Pages ({len(stale)}) — warn"]
    for s in stale:
        lines.append(f"- `{s['file']}` — last updated {s['updated']}")
    lines += ["", f"## Unverified Pages ({len(unverified)}) — warn (TODO/verify markers)"]
    for u in unverified:
        lines.append(f"- `{u['file']}` — {u['marker_count']} marker(s)")
    lines += ["", f"## Missing bold summary ({len(no_summary)}) — warn"]
    for m in no_summary[:40]:
        lines.append(f"- `{m['file']}`")
    if len(no_summary) > 40:
        lines.append(f"- … and {len(no_summary) - 40} more")
    lines += ["", f"## Type/dir mismatch ({len(type_mismatch)}) — warn"]
    for t in type_mismatch:
        lines.append(f"- `{t['file']}` — {t['issue']}")
    lines += ["", f"## Active restricted placeholders ({len(placeholders)}) — error"]
    for item in placeholders:
        lines.append(f"- `{item['file']}` — status={item['status']}")
    lines += ["", f"## Weak sources only ({len(weak_src)}) — warn"]
    for w in weak_src[:40]:
        lines.append(f"- `{w['file']}` — {w['sources']}")
    if len(weak_src) > 40:
        lines.append(f"- … and {len(weak_src) - 40} more")

    report = "\n".join(lines)
    (wiki_dir / "lint_report.md").write_text(report, encoding="utf-8")
    return report
