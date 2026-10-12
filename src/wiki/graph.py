"""
WikiGraph — in-memory typed knowledge graph over wiki/ frontmatter links (WS-3).

Parses all wiki pages on first access, builds an adjacency structure from
`links:` frontmatter fields. Supports BFS traversal and project-scoped
knowledge retrieval.

Module-level singleton (`get_wiki_graph()`) with lock-protected lazy build.
Call `invalidate_wiki_graph()` after any wiki write to force a rebuild.
"""
from __future__ import annotations

import logging
import sqlite3
import threading
from dataclasses import dataclass, field
from pathlib import Path

from config.settings import BASE_DIR
from src.wiki.schema import WikiLink, parse_page

logger = logging.getLogger(__name__)

_WIKI_DIR = BASE_DIR / "wiki"
_EXCLUDE_DIRS = {"raw"}


@dataclass
class WikiNode:
    path: str              # relative path without extension: "entity/cms"
    title: str
    page_type: str
    tags: list[str]
    status: str
    visibility: str = "public"
    visibility_explicit: bool = False
    scope: str = "global"
    project_ids: list[str] = field(default_factory=list)
    markets: list[str] = field(default_factory=list)
    counterparties: list[str] = field(default_factory=list)
    conflict_state: str = "none"
    superseded_by: str | None = None
    external_grounding: bool = False
    sensitive_kinds: list[str] = field(default_factory=list)
    outlinks: list[WikiLink] = field(default_factory=list)


class WikiGraph:
    """In-memory graph built from wiki page frontmatter `links:` fields."""

    def __init__(self, wiki_dir: Path | None = None) -> None:
        self._wiki_dir = Path(wiki_dir) if wiki_dir else _WIKI_DIR
        self._nodes: dict[str, WikiNode] = {}   # path → node
        self._inlinks: dict[str, list[str]] = {}  # path → list of source paths

    # ── Build ────────────────────────────────────────────────────────────

    def build(self) -> None:
        """Scan wiki_dir, parse all pages, build adjacency structure."""
        nodes: dict[str, WikiNode] = {}

        if not self._wiki_dir.is_dir():
            self._nodes = nodes
            self._inlinks = {}
            return

        for subdir in sorted(self._wiki_dir.iterdir()):
            if not subdir.is_dir() or subdir.name in _EXCLUDE_DIRS:
                continue
            # rglob so nested paths (playbook/settlement/*.md) enter the graph
            for md_path in sorted(subdir.rglob("*.md")):
                rel = md_path.relative_to(self._wiki_dir).as_posix()
                stem = rel.removesuffix(".md")
                try:
                    raw = md_path.read_text(encoding="utf-8")
                    page = parse_page(raw)
                    governance = page.governance
                    from src.wiki.policy import scan_sensitive_text
                    nodes[stem] = WikiNode(
                        path=stem,
                        title=page.title or md_path.stem,
                        page_type=page.type or "unknown",
                        tags=list(page.tags),
                        status=page.status or "active",
                        visibility=page.visibility or "public",
                        visibility_explicit=page.visibility_explicit,
                        scope=governance.scope.value if governance else "global",
                        project_ids=list(governance.project_ids) if governance else [],
                        markets=list(governance.markets) if governance else [],
                        counterparties=list(governance.counterparties) if governance else [],
                        conflict_state=governance.conflict_state.value if governance else "none",
                        superseded_by=governance.superseded_by if governance else None,
                        external_grounding=governance.external_grounding if governance else False,
                        sensitive_kinds=sorted({
                            finding.kind for finding in scan_sensitive_text(
                                page.body or "", include_identifiers=True,
                            )
                        }),
                        outlinks=list(page.links),
                    )
                except Exception:
                    logger.debug("Failed to parse wiki page %s", md_path, exc_info=True)

        # Build reverse index
        inlinks: dict[str, list[str]] = {p: [] for p in nodes}
        for src_path, node in nodes.items():
            for link in node.outlinks:
                tgt = link.target
                if tgt not in inlinks:
                    inlinks[tgt] = []
                inlinks[tgt].append(src_path)

        self._nodes = nodes
        self._inlinks = inlinks

    # ── Query ────────────────────────────────────────────────────────────

    def neighbors(self, page_path: str, rel_type: str | None = None) -> list[WikiLink]:
        """Return outbound links from page_path, optionally filtered by rel_type."""
        node = self._nodes.get(page_path)
        if not node:
            return []
        if rel_type:
            return [lnk for lnk in node.outlinks if lnk.rel == rel_type]
        return list(node.outlinks)

    def traverse(self, start: str, max_depth: int = 3, *, request=None) -> list[str]:
        """BFS from start, applying retrieval eligibility to every neighbor."""
        visited: set[str] = {start}
        queue: list[tuple[str, int]] = [(start, 0)]
        result: list[str] = []
        while queue:
            current, depth = queue.pop(0)
            if depth >= max_depth:
                continue
            node = self._nodes.get(current)
            if node is None:
                continue
            for lnk in node.outlinks:
                tgt = lnk.target
                target_node = self._nodes.get(tgt)
                if request is not None and target_node is not None:
                    from src.wiki.retrieval import is_metadata_eligible
                    if not is_metadata_eligible(
                        mode=request.mode,
                        status=target_node.status,
                        visibility=target_node.visibility,
                        visibility_explicit=target_node.visibility_explicit,
                        scope=target_node.scope,
                        project_ids=target_node.project_ids,
                        markets=target_node.markets,
                        counterparties=target_node.counterparties,
                        conflict_state=target_node.conflict_state,
                        superseded_by=target_node.superseded_by,
                        external_grounding=target_node.external_grounding,
                        sensitive_kinds=target_node.sensitive_kinds,
                        request_project_id=request.project_id,
                        request_market=request.market,
                        request_counterparties=request.counterparties,
                    ):
                        continue
                if tgt not in visited:
                    visited.add(tgt)
                    result.append(tgt)
                    queue.append((tgt, depth + 1))
        return result

    def impact(self, page_path: str) -> list[str]:
        """Return paths of pages that link TO page_path (reverse graph)."""
        return list(self._inlinks.get(page_path, []))

    def related_to_project(
        self,
        project_id: str,
        conn: sqlite3.Connection,
        max_results: int = 10,
    ) -> list[dict]:
        """Return wiki pages relevant to a project via entity/market traversal.

        Looks up client_name and market from the projects table, finds matching
        entity and market pages, traverses up to depth 2, deduplicates.
        """
        row = conn.execute(
            "SELECT client_name, market FROM projects WHERE project_id = ?",
            (project_id,),
        ).fetchone()
        if not row:
            return []
        client_name = (row["client_name"] or "").lower()
        market = (row["market"] or "").lower()
        first_token = client_name.split()[0] if client_name else ""

        seed_paths: list[str] = []

        # Find entity pages matching client name
        for path, node in self._nodes.items():
            if node.page_type == "entity":
                name_match = client_name and (
                    client_name in node.title.lower()
                    or node.title.lower() in client_name
                    or (first_token and first_token in node.title.lower())
                    or any(
                        client_name in t.lower() or t.lower() in client_name
                        for t in node.tags
                    )
                )
                if name_match:
                    seed_paths.append(path)

        # Find market pages matching project market
        if market:
            for path, node in self._nodes.items():
                if node.page_type == "market" and market in path.lower():
                    seed_paths.append(path)

        if not seed_paths:
            return []

        # BFS from seed pages (depth 2)
        seen: set[str] = set(seed_paths)
        result_paths = list(seed_paths)
        for seed in seed_paths:
            for neighbor_path in self.traverse(seed, max_depth=2):
                if neighbor_path not in seen:
                    seen.add(neighbor_path)
                    result_paths.append(neighbor_path)

        # Build result dicts
        results: list[dict] = []
        for path in result_paths[:max_results]:
            node = self._nodes.get(path)
            if node:
                results.append({
                    "path": path,
                    "title": node.title,
                    "type": node.page_type,
                    "tags": node.tags,
                    "status": node.status,
                })
        return results

    def invalidate(self) -> None:
        """Signal that the graph should be rebuilt on next access."""
        invalidate_wiki_graph()


# ── Singleton ─────────────────────────────────────────────────────────────

_wiki_graph: WikiGraph | None = None
_graph_lock = threading.Lock()


def get_wiki_graph() -> WikiGraph:
    """Return the cached WikiGraph, building it on first access."""
    global _wiki_graph
    if _wiki_graph is not None:
        return _wiki_graph
    with _graph_lock:
        if _wiki_graph is None:
            _wiki_graph = WikiGraph()
            _wiki_graph.build()
    return _wiki_graph


def invalidate_wiki_graph() -> None:
    """Clear the cached graph. Next call to get_wiki_graph() will rebuild."""
    global _wiki_graph
    with _graph_lock:
        _wiki_graph = None
