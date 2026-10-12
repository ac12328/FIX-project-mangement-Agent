"""Centralized safe wiki path resolution and mutation helpers."""
from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Callable, TypeVar

from config.settings import BASE_DIR

_WIKI_ROOT = BASE_DIR / "wiki"
_SAFE_PART = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]*$")
_T = TypeVar("_T")


class WikiPathError(ValueError):
    """Raised when a wiki-relative path is unsafe or malformed."""


def normalize_wiki_path(path: str, *, require_category: bool = True) -> str:
    """Return a canonical extensionless wiki path such as ``concept/fix-tags``."""
    raw = (path or "").strip().replace("\\", "/").strip("/")
    if raw.lower().endswith(".md"):
        raw = raw[:-3]
    parts = raw.split("/") if raw else []
    if not parts or (require_category and len(parts) < 2):
        raise WikiPathError("path must be 'category/stem'")
    if any(part in {"", ".", ".."} or not _SAFE_PART.fullmatch(part) for part in parts):
        raise WikiPathError("path contains unsafe characters or traversal")
    return "/".join(parts)


def resolve_wiki_path(
    path: str,
    *,
    wiki_dir: Path | str | None = None,
    require_category: bool = True,
) -> tuple[str, Path]:
    """Resolve a wiki-relative path and prove the result remains under the wiki root."""
    canonical = normalize_wiki_path(path, require_category=require_category)
    root = Path(wiki_dir) if wiki_dir is not None else _WIKI_ROOT
    root = root.resolve()
    target = (root / f"{canonical}.md").resolve()
    if os.path.commonpath((str(root), str(target))) != str(root):
        raise WikiPathError("path escapes wiki root")
    return canonical, target


def invalidate_wiki_caches() -> None:
    """Invalidate all known wiki-derived singleton caches."""
    from src.wiki.graph import invalidate_wiki_graph
    from src.wiki.indexer import invalidate_wiki_index

    invalidate_wiki_graph()
    invalidate_wiki_index()
    try:
        from src.wiki.domain_index import invalidate_wiki_domain_index
        invalidate_wiki_domain_index()
    except Exception:
        pass


def mutate_wiki_file(path: Path, mutation: Callable[[], _T]) -> _T:
    """Run a filesystem mutation and invalidate caches only after it succeeds."""
    result = mutation()
    invalidate_wiki_caches()
    return result


def collision_safe_path(path: str, *, wiki_dir: Path | str | None = None) -> tuple[str, Path]:
    """Resolve a create path, adding a numeric suffix when the target already exists."""
    canonical, target = resolve_wiki_path(path, wiki_dir=wiki_dir)
    if not target.exists():
        return canonical, target
    category, stem = canonical.rsplit("/", 1)
    suffix = 2
    while True:
        candidate = f"{category}/{stem}-{suffix}"
        canonical, target = resolve_wiki_path(candidate, wiki_dir=wiki_dir)
        if not target.exists():
            return canonical, target
        suffix += 1
