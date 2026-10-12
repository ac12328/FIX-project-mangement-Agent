"""Deterministic validation authority for Wiki publication candidates."""
from __future__ import annotations

import os
import re
import shutil
import tempfile
from pathlib import Path

from src.wiki.policy import scan_sensitive_text
from src.wiki.schema import parse_page
from src.wiki.write_guard import WikiValidationError, WikiWriteGuard

_INJECTION = re.compile(r"ignore\s+(?:all\s+)?previous|system\s+prompt|write\s+(?:to|file)|execute\s+(?:command|code)", re.I)
_ALLOWED_ROOTS = {"entity", "markets", "concept", "pattern", "playbook", "sources", "insight"}
_ERROR_HEADERS = ("## Dead Links (", "## Typed link issues (", "## Archive redirect issues (",
                  "## Missing from index.md (", "## Legacy plural directory pages (",
                  "## Active restricted placeholders (")


def validate_path(path: str) -> str:
    normalized = (path or "").replace("\\", "/").removesuffix(".md").strip("/")
    if not normalized or os.path.isabs(normalized) or ".." in normalized.split("/"):
        raise ValueError("unsafe Wiki publication path")
    if normalized.split("/", 1)[0] not in _ALLOWED_ROOTS or ":" in normalized:
        raise ValueError("Wiki publication path is outside the allowlist")
    return normalized


def validate_untrusted_fields(*, path: str, title: str, body: str) -> None:
    validate_path(path)
    combined = f"{title}\n{body}"
    if _INJECTION.search(combined):
        raise ValueError("untrusted content contains instruction injection")
    if scan_sensitive_text(combined, include_identifiers=True):
        raise ValueError("untrusted content contains secret-like material")


def safe_target(wiki_dir: Path, canonical_path: str) -> Path:
    target = (wiki_dir / f"{validate_path(canonical_path)}.md").resolve()
    try:
        target.relative_to(wiki_dir.resolve())
    except ValueError as exc:
        raise ValueError("Wiki target escapes publication root") from exc
    return target


def validate_staged_root(wiki_dir: Path, *, canonical_path: str, expected_hash: str) -> list[str]:
    """Run official lint plus page, index, and graph checks against a candidate root."""
    from src.wiki.graph import WikiGraph
    from src.wiki.index_sync import rebuild_index
    from src.wiki.indexer import WikiIndex
    from src.wiki.lint import run_lint

    target = safe_target(wiki_dir, canonical_path)
    if not target.exists():
        return ["published page is missing"]
    content = target.read_text(encoding="utf-8")
    page = parse_page(content)
    lint_page = parse_page(content)
    lint_page.body = re.sub(r"^\s*<!-- wiki-publication:[^>]+:start -->\s*", "", lint_page.body or "")
    lint_page.body = re.sub(r"\s*<!-- wiki-publication:[^>]+:end -->\s*$", "", lint_page.body or "")
    issues: list[str] = []
    try:
        WikiWriteGuard().validate(lint_page, path=canonical_path, require_summary=True,
                                  require_sources=True, strict_sources=True, check_type_dir=True)
    except WikiValidationError as exc:
        issues.append(str(exc))
    if not page.governance or page.governance.content_hash != expected_hash:
        issues.append("governance content hash mismatch")
    rebuild_index(wiki_dir)
    report = run_lint(wiki_dir)
    for header in _ERROR_HEADERS:
        match = re.search(re.escape(header) + r"(\d+)\)", report)
        if match and int(match.group(1)):
            issues.append(f"official lint failed: {header[3:].split(' (')[0]}")
    index = WikiIndex(wiki_dir=wiki_dir); index.build()
    index_path = wiki_dir / "index.md"
    index_text = index_path.read_text(encoding="utf-8") if index_path.exists() else ""
    if canonical_path not in index_text and not any(chunk.file == target.name for chunk in index.chunks):
        issues.append("target page missing from rebuilt search index")
    graph = WikiGraph(wiki_dir=wiki_dir); graph.build()
    if canonical_path not in graph._nodes:
        issues.append("target page missing from rebuilt graph")
    return issues


def stage_candidate_root(wiki_dir: Path) -> Path:
    parent = wiki_dir.resolve().parent
    staged = Path(tempfile.mkdtemp(prefix=f".{wiki_dir.name}.publication-", dir=parent))
    if wiki_dir.exists():
        shutil.copytree(wiki_dir, staged, dirs_exist_ok=True)
    return staged


# Backward-compatible single-page entry used by existing tests.
def validate_published_page(path: Path, *, expected_path: str, expected_hash: str) -> list[str]:
    return validate_staged_root(path.parents[len(Path(expected_path).parts) - 1],
                                canonical_path=expected_path, expected_hash=expected_hash)
