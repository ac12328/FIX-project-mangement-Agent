"""Canonical wiki corpus enumeration and legacy-copy reconciliation."""
from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path

from src.wiki.policy import scan_sensitive_text
from src.wiki.schema import CANONICAL_CONTENT_DIRS, WikiPage, parse_page

_EXCLUDE_DIRS = {"raw", ".obsidian"}
_EXCLUDE_FILES = {
    "index.md", "log.md", "schema.md", "lint_report.md", "README.md",
    "WIKI_GUIDE.md", "graph-index.md",
}


@dataclass(frozen=True)
class CorpusPage:
    path: str
    file_path: Path
    page: WikiPage
    content_hash: str
    body_hash: str
    sensitive_kinds: tuple[str, ...]


@dataclass(frozen=True)
class LegacyCopy:
    path: str
    file_path: Path
    content_hash: str
    canonical_path: str
    relationship: str


def _hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def normalize_body(text: str) -> str:
    return " ".join((text or "").lower().split())


def iter_canonical_pages(wiki_dir: Path):
    """Yield canonical Markdown pages only."""
    for top_dir in sorted(CANONICAL_CONTENT_DIRS - {"raw"}):
        root = wiki_dir / top_dir
        if not root.is_dir():
            continue
        for path in sorted(root.rglob("*.md")):
            rel = path.relative_to(wiki_dir)
            if any(part in _EXCLUDE_DIRS for part in rel.parts):
                continue
            if path.name in _EXCLUDE_FILES:
                continue
            raw = path.read_text(encoding="utf-8", errors="replace")
            page = parse_page(raw)
            yield CorpusPage(
                path=rel.as_posix().removesuffix(".md"),
                file_path=path,
                page=page,
                content_hash=_hash(raw),
                body_hash=_hash(normalize_body(page.body or "")),
                sensitive_kinds=tuple(sorted({
                    finding.kind for finding in scan_sensitive_text(
                        page.body or "", include_identifiers=True,
                    )
                })),
            )


def list_canonical_pages(wiki_dir: Path) -> list[CorpusPage]:
    return list(iter_canonical_pages(wiki_dir))


def _legacy_relationship(legacy_text: str, canonical_text: str | None) -> str:
    if canonical_text is None:
        return "orphan_legacy"
    legacy_norm = normalize_body(legacy_text)
    canonical_norm = normalize_body(canonical_text)
    if legacy_norm == canonical_norm:
        return "exact_duplicate"
    if legacy_norm and legacy_norm in canonical_norm:
        return "canonical_superset"
    if canonical_norm and canonical_norm in legacy_norm:
        return "legacy_superset"
    return "diverged"


def list_legacy_copies(wiki_dir: Path) -> list[LegacyCopy]:
    """Return extensionless files under content dirs with canonical relationship."""
    copies: list[LegacyCopy] = []
    for top_dir in sorted(CANONICAL_CONTENT_DIRS - {"raw"}):
        root = wiki_dir / top_dir
        if not root.is_dir():
            continue
        for path in sorted(root.rglob("*")):
            if not path.is_file() or path.suffix or path.name in _EXCLUDE_FILES:
                continue
            rel = path.relative_to(wiki_dir).as_posix()
            canonical = path.with_suffix(".md")
            text = path.read_text(encoding="utf-8", errors="replace")
            canonical_text = (
                canonical.read_text(encoding="utf-8", errors="replace")
                if canonical.is_file() else None
            )
            copies.append(LegacyCopy(
                path=rel,
                file_path=path,
                content_hash=_hash(text),
                canonical_path=f"{rel}.md",
                relationship=_legacy_relationship(text, canonical_text),
            ))
    return copies
