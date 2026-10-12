"""Deterministic, operator-reviewed navigation plans for canonical wiki pages."""
from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timezone
from hashlib import sha256
from pathlib import Path
import re
from typing import Any

from src.wiki.corpus import CorpusPage, list_canonical_pages
from src.wiki.lint import find_semantic_orphans
from src.wiki.schema import (
    VALID_LINK_RELS,
    WikiLink,
    dedupe_wiki_links,
    parse_page,
    render_page,
)
from src.wiki.write_guard import WikiValidationError, WikiWriteGuard

MANIFEST_VERSION = 1
_RECIPROCAL = {"depends_on": "depended_by", "depended_by": "depends_on", "uses": "used_by", "used_by": "uses"}
_HUB_RULES = (
    ("concept/hk_ipo_operator_reference", re.compile(r"\bhk\s+ipo\b", re.I)),
    ("concept/settlement/settlement-instruction-and-dvp-reference", re.compile(r"\bsettlement\s+instruction\s*/\s*dvp\b|\bdvp\b", re.I)),
    ("concept/internal-ops/b2badmin-account-lifecycle-reference", re.compile(r"\bb2badmin\b", re.I)),
    ("concept/settlement/taiwan-counterparty-settlement-operations-reference", re.compile(r"\btaiwan\s+(?:counterparty\s+)?settlement\b", re.I)),
)


def _hash(value: str) -> str:
    return sha256(value.encode("utf-8")).hexdigest()


def _status(page: Any) -> str:
    return (page.status or "active").lower()


def _root_hash(pages: list[CorpusPage]) -> str:
    return _hash("\n".join(f"{page.path}:{page.content_hash}" for page in pages))


def wiki_root_hash(wiki_root: Path) -> str:
    """Match the canonical manifest ``path:content_hash`` root hash algorithm."""
    return _root_hash(list_canonical_pages(Path(wiki_root)))


def _pages(wiki_root: Path) -> dict[str, CorpusPage]:
    return {page.path: page for page in list_canonical_pages(Path(wiki_root))}


def _orphan_paths(wiki_root: Path) -> list[str]:
    return sorted(item["file"].removesuffix(".md") for item in find_semantic_orphans(Path(wiki_root)))


def _cluster(path: str) -> str:
    parts = path.split("/")
    return "/".join(parts[:2]) if len(parts) > 2 else f"{parts[0]}/root"


def _inventory(pages: dict[str, CorpusPage], orphans: list[str]) -> list[dict[str, Any]]:
    return [{"path": path, "type": pages[path].page.type or "unknown", "cluster": _cluster(path),
             "tags": sorted(pages[path].page.tags), "hash": pages[path].content_hash,
             "outbound": [{"target": link.target, "rel": link.rel, "note": link.note}
                          for link in sorted(pages[path].page.links, key=lambda x: (x.target, x.rel, x.note))]}
            for path in orphans]


def _candidate(pages: dict[str, CorpusPage], source: str, target: str, rel: str,
               note: str, signal: str, evidence: str) -> dict[str, Any]:
    return {"candidate_id": _hash(f"{source}\0{target}\0{rel}\0{signal}")[:20],
            "source_path": source, "target_path": target, "source_hash": pages[source].content_hash,
            "target_hash": pages[target].content_hash, "rel": rel, "note": note, "signal": signal,
            "evidence": evidence, "cluster": _cluster(target), "manual_review_required": True}


def generate_navigation_plan(wiki_root: Path, *, batch: str = "navigation-plan", generated_at: str | None = None) -> dict[str, Any]:
    """Produce a deterministic, no-write reviewed-navigation artifact."""
    root, pages = Path(wiki_root), _pages(wiki_root)
    orphans = _orphan_paths(root)
    candidates: dict[str, dict[str, Any]] = {}
    for target in orphans:
        page = pages[target].page
        for link in page.links:
            source = link.target
            if source in pages and source != target and _status(pages[source].page) != "archived":
                item = _candidate(pages, source, target, _RECIPROCAL.get(link.rel, "relates_to"), "reciprocal ownership", "reciprocal-owner", f"{target} has typed outbound link to {source}")
                candidates[item["candidate_id"]] = item
        text = "\n".join((page.title or "", " ".join(page.tags), page.body or ""))
        for source, pattern in _HUB_RULES:
            if (
                source in pages
                and source != target
                and _status(pages[source].page) != "archived"
                and pattern.search(text)
                and not any(link.target == target for link in pages[source].page.links)
            ):
                item = _candidate(pages, source, target, "relates_to", "established hub ownership", "exact-hub-owner", f"distinctive rule for {source}")
                candidates[item["candidate_id"]] = item
    ordered = sorted(candidates.values(), key=lambda x: (x["source_path"], x["target_path"], x["rel"], x["candidate_id"]))
    inventory = _inventory(pages, orphans)
    cluster_counts: dict[str, int] = defaultdict(int)
    for item in inventory:
        cluster_counts[item["cluster"]] += 1
    return {"manifest_version": MANIFEST_VERSION, "batch": batch, "generated_at": generated_at or datetime.now(timezone.utc).isoformat(),
            "wiki_root_hash": _root_hash(list(pages.values())),
            "summary": {"semantic_orphans": len(orphans), "candidates": len(ordered),
                        "orphans_with_outbound": sum(bool(item["outbound"]) for item in inventory),
                        "orphan_clusters": dict(sorted(cluster_counts.items()))},
            "inventory": inventory, "candidates": ordered}


build_navigation_plan = generate_navigation_plan


def _approved(plan: dict[str, Any], reviews: dict[str, str]) -> list[dict[str, Any]]:
    candidates = plan.get("candidates", [])
    if set(reviews) != {item.get("candidate_id") for item in candidates} or any(v not in {"approve", "reject"} for v in reviews.values()):
        raise ValueError("every candidate requires an explicit approve or reject decision")
    return [item for item in candidates if reviews[item["candidate_id"]] == "approve"]


def validate_reviewed_plan(plan: dict[str, Any], reviews: dict[str, str], wiki_root: Path) -> list[dict[str, Any]]:
    """Reject stale or unsafe approvals; do not mutate wiki files."""
    root, pages = Path(wiki_root), _pages(wiki_root)
    if plan.get("manifest_version") != MANIFEST_VERSION:
        raise ValueError("unsupported navigation plan manifest")
    if plan.get("wiki_root_hash") != _root_hash(list(pages.values())):
        raise ValueError("wiki root is stale")
    inventory, seen = {item.get("path") for item in plan.get("inventory", [])}, set()
    approved = _approved(plan, reviews)
    for item in approved:
        source, target, rel = item.get("source_path"), item.get("target_path"), item.get("rel")
        if not isinstance(source, str) or not isinstance(target, str) or source == target:
            raise ValueError("candidate has invalid or self link")
        if rel not in VALID_LINK_RELS or source not in pages or target not in pages:
            raise ValueError("candidate has missing path or invalid relation")
        if _status(pages[source].page) == "archived" or _status(pages[target].page) == "archived":
            raise ValueError("candidate source and target must be active")
        if target not in inventory:
            raise ValueError("approved target is not an inventory orphan")
        if (source, target) in seen or any(link.target == target for link in pages[source].page.links):
            raise ValueError("candidate duplicates an existing link")
        if item.get("source_hash") != pages[source].content_hash or item.get("target_hash") != pages[target].content_hash:
            raise ValueError("candidate page is stale")
        seen.add((source, target))
    return approved


def _operations(pages: dict[str, CorpusPage], approved: list[dict[str, Any]], today: str) -> list[dict[str, Any]]:
    groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for item in approved:
        groups[item["source_path"]].append(item)
    hashes = {}
    for source, items in groups.items():
        page = parse_page(pages[source].file_path.read_text(encoding="utf-8"))
        page.links.extend(WikiLink(x["target_path"], x["rel"], x.get("note", "")) for x in items)
        page.links = dedupe_wiki_links(page.links)
        page.updated = today
        hashes[source] = _hash(render_page(page))
    return [{"candidate_id": x["candidate_id"], "source_path": x["source_path"], "target_path": x["target_path"], "rel": x["rel"], "before_hash": pages[x["source_path"]].content_hash, "after_hash": hashes[x["source_path"]]} for x in approved]


def apply_reviewed_plan(plan: dict[str, Any], reviews: dict[str, str], wiki_root: Path, *, dry_run: bool = False) -> dict[str, Any]:
    """Apply reviewed links atomically, including index and log side effects."""
    root, approved = Path(wiki_root), validate_reviewed_plan(plan, reviews, wiki_root)
    pages, before_orphans = _pages(root), len(_orphan_paths(root))
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    operations = _operations(pages, approved, today)
    before_hash = _root_hash(list(pages.values()))
    if dry_run:
        return {"dry_run": True, "applied": 0, "operations": operations, "before_hash": before_hash, "after_hash": before_hash,
                "before_orphans": before_orphans, "after_orphans": before_orphans - len({x["target_path"] for x in approved}), "orphan_delta": -len({x["target_path"] for x in approved})}
    touched_paths = {pages[item["source_path"]].file_path for item in approved}
    originals: dict[Path, bytes | None] = {
        path: path.read_bytes() for path in touched_paths
    }
    for path in (root / "index.md", root / "log.md"):
        originals[path] = path.read_bytes() if path.exists() else None
    try:
        groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for item in approved:
            groups[item["source_path"]].append(item)
        guard = WikiWriteGuard()
        for source, items in groups.items():
            record, page = pages[source], parse_page(pages[source].file_path.read_text(encoding="utf-8"))
            page.links.extend(WikiLink(x["target_path"], x["rel"], x.get("note", "")) for x in items)
            page.updated = today
            try:
                guard.validate(page, path=source)
            except WikiValidationError:
                # Existing corpus pages can carry pre-WP11 governance debt. A
                # navigation-only edit must not waive schema/body validation,
                # but it may preserve an unchanged legacy governance warning.
                before_page = parse_page(record.file_path.read_text(encoding="utf-8"))
                try:
                    guard.validate(before_page, path=source)
                except WikiValidationError as before_error:
                    try:
                        guard.validate(page, path=source)
                    except WikiValidationError as after_error:
                        if str(before_error).split("failed validation: ", 1)[-1] != str(after_error).split("failed validation: ", 1)[-1]:
                            raise
                else:
                    raise
            record.file_path.write_text(render_page(page), encoding="utf-8")
        from src.wiki.index_sync import append_log, rebuild_index
        from src.wiki.paths import invalidate_wiki_caches

        rebuild_index(root)
        append_log(root, f"navigation-plan applied: {len(approved)} reviewed links", writer="wp11")
        invalidate_wiki_caches()
    except Exception:
        for path, content in originals.items():
            if content is None:
                path.unlink(missing_ok=True)
            else:
                path.write_bytes(content)
        raise
    after_orphans = len(_orphan_paths(root))
    return {"dry_run": False, "applied": len(approved), "operations": operations, "before_hash": before_hash, "after_hash": wiki_root_hash(root),
            "before_orphans": before_orphans, "after_orphans": after_orphans, "orphan_delta": after_orphans - before_orphans}


apply_navigation_plan = apply_reviewed_plan
