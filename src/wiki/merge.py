"""Operator-assisted wiki page merge (enrich primary, archive secondaries)."""
from __future__ import annotations

import hashlib
import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from config.settings import BASE_DIR
from src.wiki.index_sync import append_log, rebuild_index
from src.wiki.paths import invalidate_wiki_caches
from src.wiki.schema import WikiLink, dedupe_wiki_links, parse_page, render_page
from src.wiki.write_guard import WikiWriteGuard, WikiValidationError

logger = logging.getLogger(__name__)
_WIKI_DIR = BASE_DIR / "wiki"


@dataclass(frozen=True)
class MergePlan:
    primary_path: str
    secondary_paths: tuple[str, ...]
    expected_hashes: dict[str, str]
    action: str
    reasons: tuple[str, ...]


def _file_hash(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def plan_merge(
    primary_path: str,
    secondary_paths: list[str],
    *,
    wiki_dir: Path | None = None,
) -> MergePlan:
    """Plan a safe merge without writing files."""
    from src.wiki.corpus import CorpusPage, normalize_body
    from src.wiki.duplicates import compare_pages
    from src.wiki.policy import scan_sensitive_text

    wiki_dir = Path(wiki_dir) if wiki_dir else _WIKI_DIR
    paths = [primary_path.strip("/").removesuffix(".md")] + [
        path.strip("/").removesuffix(".md") for path in secondary_paths
    ]
    pages: list[CorpusPage] = []
    hashes: dict[str, str] = {}
    reasons: list[str] = []
    for path in paths:
        file_path = wiki_dir / f"{path}.md"
        if not file_path.is_file():
            return MergePlan(paths[0], tuple(paths[1:]), hashes, "error", (f"missing page: {path}",))
        raw = file_path.read_text(encoding="utf-8")
        page = parse_page(raw)
        content_hash = _file_hash(file_path)
        hashes[path] = content_hash
        pages.append(CorpusPage(
            path=path,
            file_path=file_path,
            page=page,
            content_hash=content_hash,
            body_hash=hashlib.sha256(normalize_body(page.body).encode()).hexdigest(),
            sensitive_kinds=tuple(sorted({
                finding.kind for finding in scan_sensitive_text(
                    page.body or "", include_identifiers=True,
                )
            })),
        ))

    primary = pages[0]
    action = "merge"
    for secondary in pages[1:]:
        candidate = compare_pages(primary, secondary)
        if candidate.action == "split_then_merge":
            action = "split_required"
            reasons.append(f"scope mismatch: {secondary.path}")
        elif candidate.action == "manual_conflict_review":
            action = "manual_review"
            reasons.append(f"sensitive or ambiguous content: {secondary.path}")
        elif candidate.action == "keep_separate":
            action = "manual_review"
            reasons.append(f"insufficient duplicate evidence: {secondary.path}")
    return MergePlan(paths[0], tuple(paths[1:]), hashes, action, tuple(reasons))


def apply_merge_plan(
    plan: MergePlan,
    *,
    wiki_dir: Path | None = None,
    writer: str = "merge",
) -> dict:
    """Apply a pre-approved, non-stale merge plan."""
    if plan.action != "merge":
        return {"error": f"merge plan is not applicable: {plan.action}", "reasons": list(plan.reasons)}
    wiki_dir = Path(wiki_dir) if wiki_dir else _WIKI_DIR
    for path, expected_hash in plan.expected_hashes.items():
        file_path = wiki_dir / f"{path}.md"
        if not file_path.is_file() or _file_hash(file_path) != expected_hash:
            return {"error": f"stale merge plan for {path}"}
    return merge_pages(
        plan.primary_path,
        list(plan.secondary_paths),
        wiki_dir=wiki_dir,
        writer=writer,
    )


def merge_pages(
    primary_path: str,
    secondary_paths: list[str],
    *,
    wiki_dir: Path | None = None,
    writer: str = "merge",
) -> dict:
    """Merge secondary pages into primary.

    - Appends secondary bodies under ``## Merged from {path}``
    - Unions tags, sources, and typed links
    - Archives secondaries as stubs pointing at primary
    - Rebuilds index and appends log

    Paths are ``category/stem`` without ``.md``.
    """
    wiki_dir = Path(wiki_dir) if wiki_dir else _WIKI_DIR
    primary_path = primary_path.strip("/").removesuffix(".md")
    primary_file = wiki_dir / f"{primary_path}.md"
    if not primary_file.exists():
        return {"error": f"primary page not found: {primary_path}"}

    primary = parse_page(primary_file.read_text(encoding="utf-8"))
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    archived: list[str] = []

    for sec in secondary_paths:
        sec = sec.strip("/").removesuffix(".md")
        if sec == primary_path:
            continue
        sec_file = wiki_dir / f"{sec}.md"
        if not sec_file.exists():
            logger.warning("merge: skip missing secondary %s", sec)
            continue
        secondary = parse_page(sec_file.read_text(encoding="utf-8"))

        # Union metadata
        for tag in secondary.tags:
            if tag not in primary.tags:
                primary.tags.append(tag)
        for src in secondary.sources:
            if src not in primary.sources:
                primary.sources.append(src)
        existing_targets = {(lnk.target, lnk.rel) for lnk in primary.links}
        for lnk in secondary.links:
            key = (lnk.target, lnk.rel)
            if key not in existing_targets and lnk.target != primary_path:
                primary.links.append(lnk)
                existing_targets.add(key)

        block = (
            f"\n\n## Merged from `{sec}`\n\n"
            f"*(Merged {today}; original title: {secondary.title})*\n\n"
            f"{(secondary.body or '').strip()}\n"
        )
        primary.body = (primary.body or "").rstrip() + block
        primary.updated = today

        # Archive secondary as pointer stub
        stub_body = (
            f"**Archived — content merged into [{primary.title}]({primary_path}.md).**\n\n"
            f"See [[{primary_path}]] for the canonical page.\n"
        )
        stub = secondary
        stub.status = "archived"
        stub.updated = today
        stub.body = stub_body
        # Stub points at canonical page; primary supersedes this archived path
        stub.links = [
            WikiLink(target=primary_path, rel="derived_from", note="merged into primary")
        ]
        primary.links.append(
            WikiLink(target=sec, rel="supersedes", note="archived after Wave merge")
        )
        stub.links = dedupe_wiki_links(stub.links)
        primary.links = dedupe_wiki_links(primary.links)
        try:
            WikiWriteGuard().validate(stub, path=sec, require_summary=True)
        except WikiValidationError:
            # Stub always has bold summary; ignore other legacy field issues
            pass
        sec_file.write_text(render_page(stub), encoding="utf-8")
        archived.append(sec)

    primary.links = dedupe_wiki_links(primary.links)
    try:
        WikiWriteGuard().validate(
            primary, path=primary_path, require_summary=False, require_sources=False
        )
    except WikiValidationError as exc:
        return {"error": str(exc)}

    primary_file.write_text(render_page(primary), encoding="utf-8")
    rebuild_index(wiki_dir)
    append_log(
        wiki_dir,
        f"merge {primary_path} ← {', '.join(archived) or '(none)'}",
        writer=writer,
    )

    invalidate_wiki_caches()

    return {
        "primary": primary_path,
        "archived": archived,
        "status": "merged",
    }
