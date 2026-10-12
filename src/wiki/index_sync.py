"""Maintain wiki/index.md and wiki/log.md after writes."""
from __future__ import annotations

import logging
import re
import threading
from datetime import datetime, timezone
from pathlib import Path

from config.settings import BASE_DIR
from src.wiki.schema import TYPE_TO_DIR, parse_page

logger = logging.getLogger(__name__)

_WIKI_DIR = BASE_DIR / "wiki"
_EXCLUDE_FILES = frozenset({
    "index.md", "log.md", "schema.md", "lint_report.md", "README.md",
    "WIKI_GUIDE.md", "graph-index.md",
})
_EXCLUDE_DIRS = frozenset({"raw", ".obsidian"})
_INDEX_LOCK = threading.Lock()

# Section order for index.md
_TYPE_SECTIONS: list[tuple[str, str]] = [
    ("market", "Markets"),
    ("entity", "Entities"),
    ("concept", "Concepts"),
    ("pattern", "Patterns"),
    ("playbook", "Playbooks"),
    ("source", "Sources"),
    ("insight", "Insights"),
]

_PM_MANUAL_ENTRIES: list[tuple[str, str]] = [
    ("playbook/fix-connectivity/fix-onboarding-pm-manual", "Start: FIX Onboarding PM Manual"),
    ("playbook/fix-connectivity/fix-onboarding-milestone-pm-guide", "M1-M8 Milestone Guide"),
    ("playbook/fix-connectivity/milestone-completion-criteria", "Milestone Completion Criteria"),
    ("concept/fix_troubleshooting", "FIX Troubleshooting"),
    ("playbook/production-fix-session-setup-sequence", "Production and PCT Preparation"),
]


def _content_pages(wiki_dir: Path) -> list[tuple[str, str, str]]:
    """Return list of (path_no_ext, title, page_type) for content pages."""
    pages: list[tuple[str, str, str]] = []
    if not wiki_dir.is_dir():
        return pages

    for subdir in sorted(wiki_dir.iterdir()):
        if not subdir.is_dir() or subdir.name in _EXCLUDE_DIRS:
            continue
        for md_path in sorted(subdir.rglob("*.md")):
            if md_path.name in _EXCLUDE_FILES:
                continue
            rel = md_path.relative_to(wiki_dir).as_posix()
            path_no_ext = rel.removesuffix(".md")
            try:
                raw = md_path.read_bytes()
                text = None
                for enc in ("utf-8", "utf-8-sig", "cp1252", "latin-1"):
                    try:
                        text = raw.decode(enc)
                        break
                    except UnicodeDecodeError:
                        continue
                if text is None:
                    text = raw.decode("utf-8", errors="replace")
                page = parse_page(text)
            except Exception:
                logger.debug("index_sync: skip unreadable %s", rel, exc_info=True)
                continue
            title = page.title or md_path.stem.replace("_", " ").replace("-", " ")
            ptype = page.type or ""
            if not ptype:
                # Infer from top-level dir
                top = path_no_ext.split("/", 1)[0]
                for t, d in TYPE_TO_DIR.items():
                    if d == top:
                        ptype = t
                        break
                if not ptype:
                    ptype = "concept"
            status = (page.status or "active").lower()
            pages.append((path_no_ext, title, ptype, status))
    return pages


def list_wiki_pages(
    wiki_dir: Path | None = None,
    *,
    include_archived: bool = True,
) -> list[tuple[str, str, str]]:
    """Public: (path_no_ext, title, type) for content pages.

    By default includes archived stubs (redirects). Pass include_archived=False
    for catalog/curator contexts that should prefer live pages only.
    """
    rows = _content_pages(Path(wiki_dir) if wiki_dir else _WIKI_DIR)
    if include_archived:
        return [(p, t, ty) for p, t, ty, _st in rows]
    return [(p, t, ty) for p, t, ty, st in rows if st != "archived"]


def build_catalog_text(wiki_dir: Path | None = None, *, max_lines: int = 400) -> str:
    """Compact catalog for curator LLM: `- path | title | type` (active pages first)."""
    pages = list_wiki_pages(wiki_dir, include_archived=False)
    lines = ["# Wiki catalog (path | title | type)", ""]
    # Prefer balanced sample if over max_lines: keep all entities/markets first
    if len(pages) <= max_lines:
        for path, title, ptype in pages:
            lines.append(f"- {path} | {title} | {ptype}")
        return "\n".join(lines)

    priority = {"market": 0, "entity": 1, "playbook": 2, "pattern": 3, "concept": 4}
    pages_sorted = sorted(pages, key=lambda p: (priority.get(p[2], 9), p[0]))
    for path, title, ptype in pages_sorted[:max_lines]:
        lines.append(f"- {path} | {title} | {ptype}")
    lines.append(f"\n??truncated ({len(pages) - max_lines} more pages; prefer update over create)")
    return "\n".join(lines)


def rebuild_index(wiki_dir: Path | None = None) -> str:
    """Rewrite index.md from disk scan. Returns new content.

    Active pages listed under type sections; archived stubs under ## Archived.
    """
    wiki_dir = Path(wiki_dir) if wiki_dir else _WIKI_DIR
    pages = _content_pages(wiki_dir)
    by_type: dict[str, list[tuple[str, str]]] = {}
    archived: list[tuple[str, str]] = []
    for path, title, ptype, status in pages:
        if status == "archived":
            archived.append((path, title))
            continue
        by_type.setdefault(ptype, []).append((path, title))

    lines = [
        "# Wiki Index",
        "",
        "<!-- Auto-maintained by src/wiki/index_sync.py ??run scripts/rebuild_wiki_index.py -->",
        "<!-- Writing rules: wiki/WIKI_GUIDE.md -->",
        "",
    ]
    active_paths = {path for path, _title, _ptype, status in pages if status != "archived"}
    quick_start = [(path, title) for path, title in _PM_MANUAL_ENTRIES if path in active_paths]
    if quick_start:
        lines.append("## FIX Onboarding PM Manual — Start Here")
        lines.append("")
        lines.append("Follow this curated path before using the alphabetical catalog below.")
        lines.append("")
        for path, title in quick_start:
            lines.append(f"- [{title}]({path}.md)")
        lines.append("")

    known_types = {t for t, _ in _TYPE_SECTIONS}
    for ptype, heading in _TYPE_SECTIONS:
        entries = sorted(by_type.get(ptype, []), key=lambda x: x[1].lower())
        if not entries:
            continue
        lines.append(f"## {heading}")
        lines.append("")
        for path, title in entries:
            lines.append(f"- [{title}]({path}.md)")
        lines.append("")

    for ptype, entries in sorted(by_type.items()):
        if ptype in known_types:
            continue
        lines.append(f"## {ptype.title() or 'Other'}")
        lines.append("")
        for path, title in sorted(entries, key=lambda x: x[1].lower()):
            lines.append(f"- [{title}]({path}.md)")
        lines.append("")

    if archived:
        lines.append("## Archived")
        lines.append("")
        lines.append("Redirect stubs from merges ??prefer the canonical target page.")
        lines.append("")
        for path, title in sorted(archived, key=lambda x: x[0]):
            lines.append(f"- [{title}]({path}.md) *(archived)*")
        lines.append("")

    content = "\n".join(lines).rstrip() + "\n"
    index_path = wiki_dir / "index.md"
    with _INDEX_LOCK:
        index_path.write_text(content, encoding="utf-8")
    logger.info(
        "index_sync: rebuilt index.md with %d active + %d archived pages",
        sum(len(v) for v in by_type.values()),
        len(archived),
    )
    return content


def ensure_index_entry(
    wiki_dir: Path | None,
    path_no_ext: str,
    title: str,
    *,
    page_type: str = "",
) -> None:
    """Ensure a bullet exists for path; falls back to full rebuild if messy.

    Never call rebuild_index while holding _INDEX_LOCK (non-reentrant lock deadlock).
    """
    wiki_dir = Path(wiki_dir) if wiki_dir else _WIKI_DIR
    path_no_ext = path_no_ext.strip("/").removesuffix(".md")
    index_path = wiki_dir / "index.md"
    link = f"]({path_no_ext}.md)"

    if not index_path.exists():
        rebuild_index(wiki_dir)
        return

    with _INDEX_LOCK:
        text = index_path.read_text(encoding="utf-8")
        if link in text or f"]({path_no_ext})" in text:
            return
        # Prefer cheap append under matching section; else rebuild outside lock
        heading = None
        for ptype, h in _TYPE_SECTIONS:
            if page_type == ptype:
                heading = h
                break
        if heading and f"## {heading}" in text:
            bullet = f"- [{title}]({path_no_ext}.md)"
            pattern = rf"(## {re.escape(heading)}\n\n)"
            new_text, n = re.subn(pattern, rf"\1{bullet}\n", text, count=1)
            if n:
                index_path.write_text(new_text, encoding="utf-8")
                return
        need_rebuild = True
    if need_rebuild:
        rebuild_index(wiki_dir)


def append_log(
    wiki_dir: Path | None,
    message: str,
    *,
    writer: str = "system",
) -> None:
    """Append one line to log.md."""
    wiki_dir = Path(wiki_dir) if wiki_dir else _WIKI_DIR
    log_path = wiki_dir / "log.md"
    day = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    line = f"- {day} [{writer}] {message.strip()}\n"
    with _INDEX_LOCK:
        if not log_path.exists():
            log_path.write_text("# Wiki Log\n\n" + line, encoding="utf-8")
            return
        with log_path.open("a", encoding="utf-8") as fh:
            fh.write(line)


def index_paths(wiki_dir: Path | None = None) -> set[str]:
    """Set of path_no_ext entries currently linked from index.md."""
    wiki_dir = Path(wiki_dir) if wiki_dir else _WIKI_DIR
    index_path = wiki_dir / "index.md"
    if not index_path.exists():
        return set()
    text = index_path.read_text(encoding="utf-8")
    found = set(re.findall(r"\]\(([^)]+?)\.md\)", text))
    # Also bare paths without .md
    found |= set(re.findall(r"\]\(([^)]+?)\)", text))
    return {p.removesuffix(".md").strip("/") for p in found}
