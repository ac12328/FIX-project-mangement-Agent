"""Wiki search index — TF-IDF (or keyword fallback) over wiki/ pages."""
from __future__ import annotations

import logging
import re
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from config.settings import BASE_DIR

logger = logging.getLogger(__name__)

_WIKI_DIR = BASE_DIR / "wiki"
_EXCLUDE_DIRS = {"raw"}

# ---------------------------------------------------------------------------
# TF-IDF / fallback helpers
# ---------------------------------------------------------------------------

try:
    from sklearn.feature_extraction.text import TfidfVectorizer  # type: ignore
    from sklearn.metrics.pairwise import cosine_similarity as _sk_cosine  # type: ignore
    _HAS_SKLEARN = True
except ImportError:  # pragma: no cover – depends on environment
    _HAS_SKLEARN = False


@dataclass
class KnowledgeChunk:
    file: str
    section: str
    content: str
    confidence: str = ""   # frontmatter: high | medium | low | ""
    status: str = ""       # frontmatter: draft | active | trusted | archived | ""
    visibility: str = "public"
    visibility_explicit: bool = False
    summary: str = ""      # bold one-liner or first paragraph from page
    title: str = ""
    page_type: str = ""
    scope: str = "global"
    project_ids: tuple[str, ...] = ()
    markets: tuple[str, ...] = ()
    counterparties: tuple[str, ...] = ()
    conflict_state: str = "none"
    superseded_by: str | None = None
    external_grounding: bool = False
    sensitive_kinds: tuple[str, ...] = ()

def _keyword_score(query: str, text: str) -> float:
    """Simple keyword overlap score used when sklearn is absent."""
    if not query or not text:
        return 0.0
    q_tokens = set(re.findall(r"\w+", query.lower()))
    t_tokens = set(re.findall(r"\w+", text.lower()))
    if not q_tokens:
        return 0.0
    overlap = q_tokens & t_tokens
    return len(overlap) / len(q_tokens)


# ---------------------------------------------------------------------------
# WikiIndex (self-contained, no base class)
# ---------------------------------------------------------------------------

class WikiIndex:
    """Chunked TF-IDF (or keyword-fallback) search over wiki/ pages."""

    def __init__(self, *, wiki_dir: str | Path | None = None) -> None:
        self.wiki_dir = Path(wiki_dir) if wiki_dir else _WIKI_DIR
        self.chunks: list[KnowledgeChunk] = []
        self._vectorizer: Any = None
        self._tfidf_matrix: Any = None
        self._built = False

    # -- parsing ------------------------------------------------------------

    @staticmethod
    def _parse_file(path: Path) -> list[KnowledgeChunk]:
        """Split a markdown file on ``## `` headers into chunks.

        Archived pages return no chunks (Wave D: exclude from agent search index).
        """
        try:
            raw = path.read_bytes()
            text = None
            for enc in ("utf-8", "utf-8-sig", "cp1252", "latin-1"):
                try:
                    text = raw.decode(enc)
                    break
                except UnicodeDecodeError:
                    continue
            if text is None:
                text = raw.decode("utf-8", errors="replace")
        except Exception as exc:
            logger.warning("Cannot read knowledge file %s: %s", path, exc)
            return []

        from src.wiki.schema import extract_summary, parse_page

        page = parse_page(text)
        # Wave D: archived stubs must not compete with canonical pages in search
        if (page.status or "").lower() == "archived":
            return []

        fm_confidence = page.confidence or ""
        fm_status = page.status or ""
        fm_visibility = page.visibility or "public"
        page_summary = extract_summary(page.body or "") or ""
        page_title = page.title or path.stem.replace("_", " ").replace("-", " ")
        page_type = page.type or ""
        governance = page.governance
        if governance:
            scope = governance.scope.value
            project_ids = tuple(governance.project_ids)
            markets = tuple(governance.markets)
            counterparties = tuple(governance.counterparties)
            conflict_state = governance.conflict_state.value
            superseded_by = governance.superseded_by
            external_grounding = governance.external_grounding
        else:
            # Legacy pages are deliberately unscoped until migration review.
            scope = "global"
            project_ids = ()
            markets = ()
            counterparties = ()
            conflict_state = "none"
            superseded_by = None
            external_grounding = False
        from src.wiki.policy import scan_sensitive_text
        sensitive_kinds = tuple(sorted({
            finding.kind
            for finding in scan_sensitive_text(page.body or "", include_identifiers=True)
        }))

        # Chunk on ## headers using normalized body + residual frontmatter-free text
        body = page.body or ""
        parts = re.split(r"(?m)^## ", body)
        chunks: list[KnowledgeChunk] = []
        file_name = path.name

        for i, part in enumerate(parts):
            part = part.strip()
            if not part:
                continue

            if i == 0:
                section = "(intro)"
                content = part
            else:
                lines = part.split("\n", 1)
                section = lines[0].strip()
                content = lines[1].strip() if len(lines) > 1 else ""

            if len(content) < 20:
                continue

            chunks.append(KnowledgeChunk(
                file=file_name,
                section=section,
                content=content,
                confidence=fm_confidence,
                status=fm_status,
                visibility=fm_visibility or "public",
                visibility_explicit=page.visibility_explicit,
                summary=page_summary,
                title=page_title,
                page_type=page_type,
                scope=scope,
                project_ids=project_ids,
                markets=markets,
                counterparties=counterparties,
                conflict_state=conflict_state,
                superseded_by=superseded_by,
                external_grounding=external_grounding,
                sensitive_kinds=sensitive_kinds,
            ))

        return chunks

    # -- build --------------------------------------------------------------

    def build(self) -> None:
        """Parse all .md files in wiki/ subdirectories and build the search index."""
        self.chunks = []
        self._vectorizer = None
        self._tfidf_matrix = None
        self._built = False

        if not self.wiki_dir.is_dir():
            logger.warning("Wiki directory does not exist: %s", self.wiki_dir)
            self._built = True
            return

        for subdir in sorted(self.wiki_dir.iterdir()):
            if not subdir.is_dir() or subdir.name in _EXCLUDE_DIRS:
                continue
            for md_path in sorted(subdir.rglob("*.md")):
                parsed = self._parse_file(md_path)
                rel = md_path.relative_to(self.wiki_dir)
                for chunk in parsed:
                    chunk.file = str(rel).replace("\\", "/")
                self.chunks.extend(parsed)

        if not self.chunks:
            self._built = True
            return

        if _HAS_SKLEARN:
            corpus = [f"{c.title} {c.section} {c.content}" for c in self.chunks]
            self._vectorizer = TfidfVectorizer(
                stop_words="english", max_features=5000, ngram_range=(1, 2),
            )
            self._tfidf_matrix = self._vectorizer.fit_transform(corpus)

        self._built = True

    # -- search -------------------------------------------------------------

    def search(
        self,
        query: str,
        *,
        top_k: int = 3,
        domain_filter: str | None = None,
        domain_mode: str = "hard",
        boost_paths: list[str] | None = None,
        retrieval_mode: str = "internal",
        request: Any | None = None,
        original_query: str | None = None,
    ) -> list[dict[str, Any]]:
        """Return the top-k most relevant chunks for *query*.

        domain_filter:
          - domain_mode=\"hard\" (default): only paths containing /{domain}/
          - domain_mode=\"soft\": full corpus; matching paths get a small boost
        boost_paths: hub/canonical paths to boost (from query_expand intents)
        """
        if not self._built:
            self.build()

        if not query or not query.strip() or not self.chunks:
            return []

        if _HAS_SKLEARN and self._vectorizer is not None:
            q_vec = self._vectorizer.transform([query])
            scores = _sk_cosine(q_vec, self._tfidf_matrix).flatten()
        else:
            scores = [_keyword_score(query, f"{c.title} {c.section} {c.content}") for c in self.chunks]

        boost_set = {p.replace("\\", "/").removesuffix(".md").strip("/") for p in (boost_paths or [])}
        boost_stems = {p.rsplit("/", 1)[-1] for p in boost_set}

        if domain_filter and domain_mode == "hard":
            eligible = [
                i for i, c in enumerate(self.chunks)
                if f"/{domain_filter}/" in f"/{c.file}"
            ]
        else:
            eligible = list(range(len(self.chunks)))

        if request is not None:
            from src.wiki.retrieval import is_metadata_eligible

            eligible = [
                i for i in eligible
                if is_metadata_eligible(
                    mode=request.mode,
                    status=self.chunks[i].status,
                    visibility=self.chunks[i].visibility,
                    visibility_explicit=self.chunks[i].visibility_explicit,
                    scope=self.chunks[i].scope,
                    project_ids=self.chunks[i].project_ids,
                    markets=self.chunks[i].markets,
                    counterparties=self.chunks[i].counterparties,
                    conflict_state=self.chunks[i].conflict_state,
                    superseded_by=self.chunks[i].superseded_by,
                    external_grounding=self.chunks[i].external_grounding,
                    sensitive_kinds=self.chunks[i].sensitive_kinds,
                    request_project_id=request.project_id,
                    request_market=request.market,
                    request_counterparties=request.counterparties,
                )
            ]
        elif retrieval_mode == "external_draft":
            from src.wiki.policy import is_external_safe_page

            eligible = [
                i for i in eligible
                if is_external_safe_page(
                    visibility=self.chunks[i].visibility,
                    visibility_explicit=self.chunks[i].visibility_explicit,
                    status=self.chunks[i].status,
                    content=self.chunks[i].content,
                )
            ]
        elif retrieval_mode != "internal":
            raise ValueError(f"Unsupported wiki retrieval mode: {retrieval_mode}")

        from config.defaults import (
            WIKI_DOMAIN_SOFT_BOOST,
            WIKI_HUB_SCORE_BOOST,
            WIKI_TYPE_PREF_BOOST,
        )
        from src.wiki.rank import (
            detect_query_intent,
            hub_boost_amount,
            market_path_boost,
            named_title_boost,
            section_preference_boost,
            specificity_boost,
            type_preference_boost,
        )

        intent = detect_query_intent(query)

        # Soft rank boosts: trust, hub (damped), specificity, domain, type, section
        adjusted: list[tuple[int, float]] = []
        for i in eligible:
            score = float(scores[i])
            if score <= 0:
                continue
            c = self.chunks[i]
            path_no_ext = c.file.replace("\\", "/").removesuffix(".md")
            stem = path_no_ext.rsplit("/", 1)[-1]
            if c.status == "trusted":
                score += 0.15
            elif c.status == "draft":
                score -= 0.25
            if c.confidence == "high":
                score += 0.1
            elif c.confidence == "low":
                score -= 0.15
            if c.section == "(intro)":
                score += 0.05
            if c.summary:
                score += 0.08
            score += hub_boost_amount(
                path_no_ext, query, boost_set, boost_stems, full_boost=WIKI_HUB_SCORE_BOOST,
            )
            score += specificity_boost(
                query, c.title or "", c.content or "", path_no_ext, boost_set, boost_stems,
            )
            score += named_title_boost(
                original_query if original_query is not None else query, c.title or "",
            )
            score += market_path_boost(query, path_no_ext)
            if domain_filter and domain_mode == "soft":
                if f"/{domain_filter}/" in f"/{c.file}":
                    score += WIKI_DOMAIN_SOFT_BOOST
            score += type_preference_boost(c.page_type or "", intent, amount=WIKI_TYPE_PREF_BOOST)
            score += section_preference_boost(c.section or "", intent)
            adjusted.append((i, score))

        scored = sorted(adjusted, key=lambda t: t[1], reverse=True)

        results: list[dict[str, Any]] = []
        for idx, score in scored[:top_k]:
            if score <= 0:
                continue
            c = self.chunks[idx]
            trust_warning: str | None = None
            if c.status in ("draft", "archived") or c.confidence == "low":
                trust_warning = (
                    f"⚠ UNVERIFIED — this page is '{c.status or 'unknown status'}' "
                    f"(confidence: {c.confidence or 'unknown'}). "
                    "Prefer trusted/canonical pages (check supersedes / hub links)."
                )
            path_no_ext = c.file.replace("\\", "/").removesuffix(".md")
            results.append({
                "file": c.file,
                "path": path_no_ext,
                "title": c.title or path_no_ext.rsplit("/", 1)[-1],
                "page_type": c.page_type or "",
                "section": c.section,
                "content": c.content,
                "score": round(float(score), 4),
                "confidence": c.confidence,
                "status": c.status,
                "visibility": c.visibility,
                "visibility_explicit": c.visibility_explicit,
                "summary": c.summary,
                "scope": c.scope,
                "project_ids": list(c.project_ids),
                "markets": list(c.markets),
                "counterparties": list(c.counterparties),
                "conflict_state": c.conflict_state,
                "superseded_by": c.superseded_by,
                "external_grounding": c.external_grounding,
                "sensitive_kinds": list(c.sensitive_kinds),
                "trust_warning": trust_warning,
            })

        return results


# ---------------------------------------------------------------------------
# Module-level helpers (singleton)
# ---------------------------------------------------------------------------

_wiki_index: WikiIndex | None = None
_wiki_lock = threading.Lock()


def get_wiki_index() -> WikiIndex:
    global _wiki_index
    if _wiki_index is not None:
        return _wiki_index
    with _wiki_lock:
        if _wiki_index is None:
            _wiki_index = WikiIndex()
            _wiki_index.build()
    return _wiki_index


def warm_wiki_index() -> int:
    """Build the singleton index now (call at bot startup). Returns chunk count."""
    idx = get_wiki_index()
    return len(idx.chunks)


def invalidate_wiki_index() -> None:
    global _wiki_index
    with _wiki_lock:
        _wiki_index = None


def _snippet(text: str, max_chars: int) -> str:
    text = (text or "").strip()
    if max_chars > 0 and len(text) > max_chars:
        return text[:max_chars] + "..."
    return text


def wiki_search_hits(
    query: str,
    *,
    top_k: int = 5,
    include_graph_neighbors: bool = False,
    exclude_visibility: str | None = None,
    domain_filter: str | None = None,
    domain_mode: str = "hard",
    max_chunk_chars: int | None = None,
    expand: bool = True,
    snippet_chars: int = 800,
    retrieval_mode: str = "internal",
    request: Any | None = None,
) -> list[dict[str, Any]]:
    """Structured wiki search hits for tools / ContextEngine."""
    from config.defaults import WIKI_CHUNK_CHARS
    from src.wiki.query_expand import expand_query

    if max_chunk_chars is None:
        max_chunk_chars = WIKI_CHUNK_CHARS

    expanded = expand_query(query) if expand else None
    search_text = expanded.text if expanded else (query or "")
    boost_paths = expanded.boost_paths if expanded else None

    idx = get_wiki_index()
    from config.defaults import WIKI_PLAYBOOK_ATTACH_CHARS, WIKI_SEARCH_OVERFETCH
    from src.wiki.pack import attach_playbook_bodies

    # Over-fetch chunks then de-dupe by page path so multi-section pages don't fill top_k
    over = max(top_k * WIKI_SEARCH_OVERFETCH, 12)
    results = idx.search(
        search_text,
        top_k=over,
        domain_filter=domain_filter,
        domain_mode=domain_mode,
        boost_paths=boost_paths,
        retrieval_mode=retrieval_mode,
        request=request,
        original_query=query,
    )
    if exclude_visibility:
        results = [r for r in results if r.get("visibility", "public") != exclude_visibility]

    hits: list[dict[str, Any]] = []
    seen_paths: set[str] = set()
    for r in results:
        path = (r.get("path") or r.get("file") or "").replace("\\", "/").removesuffix(".md")
        if path in seen_paths:
            continue
        seen_paths.add(path)
        content = r.get("content") or ""
        if max_chunk_chars > 0 and len(content) > max_chunk_chars:
            content = content[:max_chunk_chars] + "..."
        hit: dict[str, Any] = {
            "path": path,
            "file": r.get("file") or f"{path}.md",
            "title": r.get("title") or path.rsplit("/", 1)[-1],
            "page_type": r.get("page_type") or "",
            "section": r.get("section") or "",
            "summary": (r.get("summary") or "").strip(),
            "snippet": _snippet(content, snippet_chars),
            "content": content,
            "score": r.get("score"),
            "confidence": r.get("confidence") or "",
            "status": r.get("status") or "",
            "visibility": r.get("visibility") or "public",
            "visibility_explicit": bool(r.get("visibility_explicit")),
            "scope": r.get("scope") or "global",
            "project_ids": r.get("project_ids") or [],
            "markets": r.get("markets") or [],
            "counterparties": r.get("counterparties") or [],
            "conflict_state": r.get("conflict_state") or "none",
            "superseded_by": r.get("superseded_by"),
            "external_grounding": bool(r.get("external_grounding")),
            "sensitive_kinds": r.get("sensitive_kinds") or [],
            "trust_warning": r.get("trust_warning"),
            "neighbors": [],
        }
        hits.append(hit)
        if len(hits) >= top_k:
            break

    # Graph neighbors only for top 2 hits (latency + signal)
    if include_graph_neighbors and hits:
        try:
            from src.wiki.graph import get_wiki_graph
            graph = get_wiki_graph()
            prefer_rels = {
                "requires", "depends_on", "derived_from", "operates_in", "uses",
            }
            for hit in hits[:2]:
                path = hit["path"]
                node = graph._nodes.get(path)
                neighbors: list[dict[str, str]] = []
                if node:
                    ranked = sorted(
                        node.outlinks,
                        key=lambda lnk: (0 if lnk.rel in prefer_rels else 1, lnk.rel),
                    )
                    for lnk in ranked[:2]:
                        tgt = lnk.target.replace("\\", "/").removesuffix(".md")
                        nnode = graph._nodes.get(tgt)
                        if nnode and (nnode.status or "").lower() == "archived":
                            continue
                        if request is not None and nnode is not None:
                            from src.wiki.retrieval import is_metadata_eligible
                            if not is_metadata_eligible(
                                mode=request.mode,
                                status=nnode.status,
                                visibility=nnode.visibility,
                                visibility_explicit=nnode.visibility_explicit,
                                scope=nnode.scope,
                                project_ids=nnode.project_ids,
                                markets=nnode.markets,
                                counterparties=nnode.counterparties,
                                conflict_state=nnode.conflict_state,
                                superseded_by=nnode.superseded_by,
                                external_grounding=nnode.external_grounding,
                                sensitive_kinds=nnode.sensitive_kinds,
                                request_project_id=request.project_id,
                                request_market=request.market,
                                request_counterparties=request.counterparties,
                            ):
                                continue
                        neighbors.append({
                            "path": tgt,
                            "rel": lnk.rel,
                            "title": (nnode.title if nnode else tgt),
                        })
                hit["neighbors"] = neighbors
        except Exception:
            logger.debug("Graph neighbor enrichment failed", exc_info=True)

    # Playbook read-after-search: attach full Steps body for top playbook
    hits = attach_playbook_bodies(
        hits,
        query=query,
        max_playbooks=1,
        playbook_chars=WIKI_PLAYBOOK_ATTACH_CHARS,
        wiki_dir=idx.wiki_dir,
    )
    return hits


def format_wiki_hits(
    hits: list[dict[str, Any]],
    *,
    max_chunk_chars: int | None = None,
    query: str = "",
    budget: int | None = None,
) -> str:
    """Format structured hits into the agent-facing string (budget-aware pack)."""
    from config.defaults import TOOL_WIKI_KNOWLEDGE_CHARS, WIKI_CHUNK_CHARS
    from src.wiki.pack import pack_wiki_hits

    if not hits:
        return ""
    # Prefer budget packer (summaries + top playbook body)
    pack_budget = budget if budget is not None else TOOL_WIKI_KNOWLEDGE_CHARS
    if max_chunk_chars is not None and max_chunk_chars > 0:
        # legacy callers pass max_chunk_chars as rough per-hit cap; scale pack
        pack_budget = min(pack_budget, max_chunk_chars * max(1, len(hits)))
    try:
        return pack_wiki_hits(hits, budget=pack_budget, query=query)
    except Exception:
        logger.debug("pack_wiki_hits failed; falling back", exc_info=True)

    if max_chunk_chars is None:
        max_chunk_chars = WIKI_CHUNK_CHARS
    parts: list[str] = []
    for r in hits:
        conf = r.get("confidence") or "?"
        status = r.get("status") or "?"
        path = r.get("path") or r.get("file") or ""
        header = (
            f"[wiki:{path} > {r.get('section', '')}] "
            f"(score={r.get('score')} conf={conf} status={status})"
        )
        if r.get("trust_warning"):
            header = str(r["trust_warning"]) + "\n" + header
        summary = (r.get("summary") or "").strip()
        content = r.get("full_body") or r.get("content") or r.get("snippet") or ""
        if max_chunk_chars > 0 and len(content) > max_chunk_chars:
            content = content[:max_chunk_chars] + "..."
        block = f"{header}\n"
        if summary:
            block += f"**{summary}**\n"
        block += content
        for nb in r.get("neighbors") or []:
            block += (
                f"\n  -> neighbor [{nb.get('rel')}] {nb.get('path')} "
                f"({nb.get('title')})"
            )
        parts.append(block)

    return "\n\n---\n\n".join(parts)


def wiki_search(
    query: str,
    *,
    top_k: int = 5,
    include_graph_neighbors: bool = False,
    exclude_visibility: str | None = None,
    domain_filter: str | None = None,
    domain_mode: str = "hard",
    max_chunk_chars: int | None = None,
    expand: bool = True,
    retrieval_mode: str = "internal",
    request: Any | None = None,
) -> str:
    """String-formatted wiki search (backward compatible)."""
    hits = wiki_search_hits(
        query,
        top_k=top_k,
        include_graph_neighbors=include_graph_neighbors,
        exclude_visibility=exclude_visibility,
        domain_filter=domain_filter,
        domain_mode=domain_mode,
        max_chunk_chars=max_chunk_chars,
        expand=expand,
        retrieval_mode=retrieval_mode,
        request=request,
    )
    return format_wiki_hits(hits, max_chunk_chars=max_chunk_chars, query=query)
