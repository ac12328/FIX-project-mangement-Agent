"""Embedding spike: dense retrieval + hybrid RRF vs TF-IDF (not production)."""
from __future__ import annotations

import logging
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

import numpy as np

from config.settings import BASE_DIR
from src.wiki.indexer import WikiIndex, invalidate_wiki_index
from src.wiki.query_expand import expand_query
from src.wiki.schema import extract_summary, parse_page

logger = logging.getLogger(__name__)

_EXCLUDE_DIRS = frozenset({"raw", ".obsidian"})
_RRF_K = 60


@dataclass
class EmbedChunk:
    path: str
    section: str
    title: str
    text: str
    vector: np.ndarray | None = None


@dataclass
class SpikeBackend:
    name: str
    embed_documents: Callable[[list[str]], np.ndarray]
    embed_query: Callable[[str], np.ndarray]


def _norm_path(p: str) -> str:
    return p.replace("\\", "/").removesuffix(".md").strip("/")


def load_chunks(wiki_dir: Path | None = None) -> list[EmbedChunk]:
    """Load non-archived section chunks (aligned with WikiIndex exclusions)."""
    root = Path(wiki_dir) if wiki_dir else BASE_DIR / "wiki"
    chunks: list[EmbedChunk] = []
    if not root.is_dir():
        return chunks

    for subdir in sorted(root.iterdir()):
        if not subdir.is_dir() or subdir.name in _EXCLUDE_DIRS:
            continue
        for md in sorted(subdir.rglob("*.md")):
            try:
                page = parse_page(md.read_text(encoding="utf-8"))
            except Exception:
                continue
            if (page.status or "").lower() == "archived":
                continue
            rel = md.relative_to(root).as_posix().removesuffix(".md")
            title = page.title or md.stem
            body = page.body or ""
            parts = re.split(r"(?m)^## ", body)
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
                text = f"{title}\n{section}\n{content[:1500]}"
                chunks.append(
                    EmbedChunk(path=rel, section=section, title=title, text=text)
                )
    return chunks


def try_local_backend(model_name: str = "sentence-transformers/all-MiniLM-L6-v2") -> SpikeBackend | None:
    try:
        from sentence_transformers import SentenceTransformer  # type: ignore
    except ImportError:
        logger.warning("sentence-transformers not installed")
        return None

    model = SentenceTransformer(model_name)

    def embed_documents(texts: list[str]) -> np.ndarray:
        if not texts:
            return np.zeros((0, 1), dtype=np.float32)
        vecs = model.encode(texts, normalize_embeddings=True, show_progress_bar=False)
        return np.asarray(vecs, dtype=np.float32)

    def embed_query(text: str) -> np.ndarray:
        v = model.encode([text], normalize_embeddings=True, show_progress_bar=False)
        return np.asarray(v[0], dtype=np.float32)

    return SpikeBackend(name=f"local:{model_name}", embed_documents=embed_documents, embed_query=embed_query)


def build_embed_index(
    chunks: list[EmbedChunk],
    backend: SpikeBackend,
) -> np.ndarray:
    """Fill chunk.vector and return matrix (n, dim)."""
    texts = [c.text for c in chunks]
    mat = backend.embed_documents(texts)
    for i, c in enumerate(chunks):
        c.vector = mat[i]
    return mat


def embed_search(
    query: str,
    chunks: list[EmbedChunk],
    matrix: np.ndarray,
    backend: SpikeBackend,
    *,
    top_k: int = 20,
    expand: bool = True,
) -> list[str]:
    """Return ranked unique paths by cosine similarity."""
    q = expand_query(query).text if expand else query
    qv = backend.embed_query(q)
    if matrix.size == 0:
        return []
    # vectors are L2-normalized → cosine = dot
    sims = matrix @ qv
    order = np.argsort(-sims)
    paths: list[str] = []
    seen: set[str] = set()
    for i in order:
        p = chunks[int(i)].path
        if p in seen:
            continue
        seen.add(p)
        paths.append(p)
        if len(paths) >= top_k:
            break
    return paths


def tfidf_search_paths(
    query: str,
    *,
    wiki_dir: Path | None = None,
    top_k: int = 20,
) -> list[str]:
    """v1 path ranking (expand + re-rank + path de-dupe)."""
    expanded = expand_query(query)
    idx = WikiIndex(wiki_dir=wiki_dir) if wiki_dir else WikiIndex()
    if not idx._built:
        idx.build()
    raw = idx.search(
        expanded.text,
        top_k=max(top_k * 4, 20),
        boost_paths=expanded.boost_paths,
        domain_mode="soft",
    )
    paths: list[str] = []
    seen: set[str] = set()
    for r in raw:
        p = _norm_path(r.get("path") or r.get("file") or "")
        if not p or p in seen:
            continue
        seen.add(p)
        paths.append(p)
        if len(paths) >= top_k:
            break
    return paths


def rrf_fuse(
    ranked_lists: list[list[str]],
    *,
    k: int = _RRF_K,
    top_k: int = 5,
) -> list[str]:
    scores: dict[str, float] = {}
    for ranked in ranked_lists:
        for rank, doc in enumerate(ranked, start=1):
            scores[doc] = scores.get(doc, 0.0) + 1.0 / (k + rank)
    ordered = sorted(scores.items(), key=lambda x: (-x[1], x[0]))
    return [d for d, _ in ordered[:top_k]]


def _hit_rank(paths: list[str], item: dict[str, Any]) -> int | None:
    expect = [_norm_path(p) for p in (item.get("expect_paths") or [])]
    any_of = [_norm_path(p) for p in (item.get("expect_any_of") or [])]
    norms = [_norm_path(p) for p in paths]
    for i, got in enumerate(norms, start=1):
        for e in expect + any_of:
            if not e:
                continue
            if got == e or got.endswith("/" + e) or e.endswith("/" + got):
                return i
            if got.split("/")[-1] == e.split("/")[-1]:
                return i
    return None


def metrics_for_ranks(ranks: list[int | None]) -> dict[str, float]:
    n = len(ranks)
    if n == 0:
        return {"n": 0, "hit@1": 0.0, "hit@3": 0.0, "hit@5": 0.0, "mrr": 0.0}
    h1 = sum(1 for r in ranks if r is not None and r <= 1) / n
    h3 = sum(1 for r in ranks if r is not None and r <= 3) / n
    h5 = sum(1 for r in ranks if r is not None and r <= 5) / n
    mrr = sum((1.0 / r) if r else 0.0 for r in ranks) / n
    return {
        "n": n,
        "hit@1": round(h1, 4),
        "hit@3": round(h3, 4),
        "hit@5": round(h5, 4),
        "mrr": round(mrr, 4),
    }


@dataclass
class SpikeRunResult:
    backend: str
    gold: dict[str, dict[str, float]] = field(default_factory=dict)
    adversarial: dict[str, dict[str, float]] = field(default_factory=dict)
    latency_ms: dict[str, dict[str, float]] = field(default_factory=dict)
    decision: str = "NO_ADOPT"
    notes: str = ""


def _pct(xs: list[float]) -> dict[str, float]:
    if not xs:
        return {"p50": 0.0, "p95": 0.0, "mean": 0.0}
    a = np.asarray(xs, dtype=np.float64)
    return {
        "p50": round(float(np.percentile(a, 50)), 2),
        "p95": round(float(np.percentile(a, 95)), 2),
        "mean": round(float(a.mean()), 2),
    }


def _run_set(
    items: list[dict],
    *,
    wiki_dir: Path | None,
    backend: SpikeBackend | None,
    chunks: list[EmbedChunk],
    matrix: np.ndarray,
    top_k: int,
) -> tuple[dict[str, dict[str, float]], dict[str, dict[str, float]]]:
    ranks_tf: list[int | None] = []
    ranks_em: list[int | None] = []
    ranks_hy: list[int | None] = []
    lat_tf: list[float] = []
    lat_em: list[float] = []
    lat_hy: list[float] = []

    for item in items:
        q = item["query"]

        t0 = time.perf_counter()
        tf_paths = tfidf_search_paths(q, wiki_dir=wiki_dir, top_k=20)
        lat_tf.append((time.perf_counter() - t0) * 1000)

        if backend is not None and matrix.size:
            t0 = time.perf_counter()
            em_paths = embed_search(q, chunks, matrix, backend, top_k=20)
            lat_em.append((time.perf_counter() - t0) * 1000)
            t0 = time.perf_counter()
            hy_paths = rrf_fuse([tf_paths, em_paths], top_k=top_k)
            lat_hy.append((time.perf_counter() - t0) * 1000)
        else:
            em_paths = []
            hy_paths = tf_paths[:top_k]
            lat_em.append(0.0)
            lat_hy.append(0.0)

        ranks_tf.append(_hit_rank(tf_paths[:top_k], item))
        ranks_em.append(_hit_rank(em_paths[:top_k], item) if em_paths else None)
        ranks_hy.append(_hit_rank(hy_paths[:top_k], item))

    metrics = {
        "tfidf": metrics_for_ranks(ranks_tf),
        "embed": (
            metrics_for_ranks(ranks_em)
            if backend
            else {"n": len(items), "hit@1": 0.0, "hit@3": 0.0, "hit@5": 0.0, "mrr": 0.0, "skipped": True}
        ),
        "hybrid": metrics_for_ranks(ranks_hy),
    }
    latency = {
        "tfidf": _pct(lat_tf),
        "embed": _pct(lat_em),
        "hybrid": _pct(lat_hy),
    }
    return metrics, latency


def evaluate_methods(
    gold: list[dict],
    adversarial: list[dict],
    *,
    wiki_dir: Path | None = None,
    backend: SpikeBackend | None = None,
    top_k: int = 5,
) -> SpikeRunResult:
    """Compare tfidf / embed / hybrid on gold + adversarial."""
    invalidate_wiki_index()
    # Warm TF-IDF once
    _ = tfidf_search_paths("warmup", wiki_dir=wiki_dir, top_k=5)

    chunks: list[EmbedChunk] = []
    matrix = np.zeros((0, 1), dtype=np.float32)
    if backend is not None:
        chunks = load_chunks(wiki_dir)
        matrix = build_embed_index(chunks, backend)

    gold_m, gold_lat = _run_set(
        gold, wiki_dir=wiki_dir, backend=backend, chunks=chunks, matrix=matrix, top_k=top_k,
    )
    adv_m, adv_lat = _run_set(
        adversarial, wiki_dir=wiki_dir, backend=backend, chunks=chunks, matrix=matrix, top_k=top_k,
    )

    result = SpikeRunResult(
        backend=backend.name if backend else "none",
        gold=gold_m,
        adversarial=adv_m,
        latency_ms={"gold": gold_lat, "adversarial": adv_lat},
    )

    # Decision gate
    if backend is None:
        result.decision = "NO_ADOPT"
        result.notes = "No embedding backend available; TF-IDF-only baseline recorded."
        return result

    g_tf = gold_m["tfidf"]["hit@3"]
    g_hy = gold_m["hybrid"]["hit@3"]
    a_tf = adv_m["tfidf"]["hit@3"]
    a_hy = adv_m["hybrid"]["hit@3"]
    a_em = adv_m["embed"]["hit@3"]

    if g_hy + 1e-9 < g_tf - 0.03:
        result.decision = "NO_ADOPT"
        result.notes = (
            f"Hybrid regressed gold hit@3 ({g_hy:.1%} < {g_tf:.1%} - 3pp). "
            "Do not ship embeddings."
        )
    elif a_hy + 1e-9 >= a_tf + 0.10:
        result.decision = "ADOPT_HYBRID"
        result.notes = (
            f"Adversarial hit@3 hybrid {a_hy:.1%} vs TF-IDF {a_tf:.1%} "
            f"(+{(a_hy - a_tf):.1%}); gold hit@3 ok ({g_hy:.1%})."
        )
    elif a_em + 1e-9 >= a_tf + 0.10 and a_hy + 1e-9 < a_tf + 0.10:
        result.decision = "ALIAS_ONLY"
        result.notes = (
            "Embed alone helped more than hybrid; many wins may be fixable with aliases. "
            f"adv embed hit@3={a_em:.1%} tfidf={a_tf:.1%} hybrid={a_hy:.1%}."
        )
    elif a_hy > a_tf + 0.03:
        result.decision = "ALIAS_ONLY"
        result.notes = (
            f"Modest adversarial gain (hybrid {a_hy:.1%} vs {a_tf:.1%}); "
            "prefer alias expansion before production embeddings."
        )
    else:
        result.decision = "NO_ADOPT"
        result.notes = (
            f"No material adversarial gain (hybrid {a_hy:.1%} vs TF-IDF {a_tf:.1%}). "
            "Stay on v1 TF-IDF retrieval."
        )

    return result


def render_report(result: SpikeRunResult) -> str:
    lines = [
        "# Wiki Embedding Spike Report",
        "",
        f"**Backend:** `{result.backend}`",
        f"**Decision:** **{result.decision}**",
        "",
        result.notes,
        "",
        "## Gold set",
        "",
        "| Method | hit@1 | hit@3 | hit@5 | MRR |",
        "|--------|-------|-------|-------|-----|",
    ]
    for name in ("tfidf", "embed", "hybrid"):
        m = result.gold.get(name) or {}
        if m.get("skipped"):
            lines.append(f"| {name} | — | — | — | skipped |")
        else:
            lines.append(
                f"| {name} | {m.get('hit@1', 0):.1%} | {m.get('hit@3', 0):.1%} | "
                f"{m.get('hit@5', 0):.1%} | {m.get('mrr', 0):.3f} |"
            )
    lines += [
        "",
        "## Adversarial set",
        "",
        "| Method | hit@1 | hit@3 | hit@5 | MRR |",
        "|--------|-------|-------|-------|-----|",
    ]
    for name in ("tfidf", "embed", "hybrid"):
        m = result.adversarial.get(name) or {}
        if m.get("skipped"):
            lines.append(f"| {name} | — | — | — | skipped |")
        else:
            lines.append(
                f"| {name} | {m.get('hit@1', 0):.1%} | {m.get('hit@3', 0):.1%} | "
                f"{m.get('hit@5', 0):.1%} | {m.get('mrr', 0):.3f} |"
            )

    lines += ["", "## Latency (ms, search only)", ""]
    for split, block in (result.latency_ms or {}).items():
        lines.append(f"### {split}")
        lines.append("")
        lines.append("| Method | p50 | p95 | mean |")
        lines.append("|--------|-----|-----|------|")
        for name, lat in (block or {}).items():
            lines.append(
                f"| {name} | {lat.get('p50', 0):.1f} | {lat.get('p95', 0):.1f} | "
                f"{lat.get('mean', 0):.1f} |"
            )
        lines.append("")

    lines += [
        "## Criteria reminder",
        "",
        "- **ADOPT_HYBRID** if adversarial hit@3 ≥ TF-IDF +10pp and gold hit@3 drop ≤3pp",
        "- **ALIAS_ONLY** if modest gains likely fixable with expand table",
        "- **NO_ADOPT** otherwise",
        "",
    ]
    return "\n".join(lines)
