"""Mode-aware, scope-first wiki retrieval contracts and service."""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any


class WikiRetrievalMode(str, Enum):
    INTERNAL = "internal"
    PROJECT_CONTEXT = "project_context"
    EXTERNAL_DRAFT = "external_draft"
    CURATOR = "curator"


@dataclass(frozen=True)
class WikiSearchRequest:
    query: str
    mode: WikiRetrievalMode = WikiRetrievalMode.INTERNAL
    project_id: str | None = None
    market: str | None = None
    counterparties: tuple[str, ...] = ()
    domains: tuple[str, ...] = ()
    top_k: int = 5
    include_graph_neighbors: bool = False
    expand: bool = True


@dataclass(frozen=True)
class WikiSearchResult:
    request: WikiSearchRequest
    hits: list[dict[str, Any]] = field(default_factory=list)
    formatted: str = ""


def _norm(value: str | None) -> str:
    return (value or "").strip().lower()


def is_metadata_eligible(
    *,
    mode: WikiRetrievalMode,
    status: str,
    visibility: str,
    visibility_explicit: bool,
    scope: str,
    project_ids: tuple[str, ...] | list[str],
    markets: tuple[str, ...] | list[str],
    counterparties: tuple[str, ...] | list[str],
    conflict_state: str,
    superseded_by: str | None,
    external_grounding: bool,
    sensitive_kinds: tuple[str, ...] | list[str],
    request_project_id: str | None = None,
    request_market: str | None = None,
    request_counterparties: tuple[str, ...] = (),
) -> bool:
    """Return whether page metadata is eligible before relevance scoring."""
    if _norm(status) == "archived" or superseded_by:
        return False
    if _norm(conflict_state) == "unresolved":
        return False

    page_projects = {_norm(value) for value in project_ids if value}
    page_markets = {_norm(value) for value in markets if value}
    page_counterparties = {_norm(value) for value in counterparties if value}
    wanted_project = _norm(request_project_id)
    wanted_market = _norm(request_market)
    wanted_counterparties = {_norm(value) for value in request_counterparties if value}
    normalized_scope = _norm(scope) or "global"

    if normalized_scope == "project" and (
        not wanted_project or wanted_project not in page_projects
    ):
        return False
    if normalized_scope == "market" and (
        not wanted_market or wanted_market not in page_markets
    ):
        return False
    if normalized_scope == "counterparty" and (
        not wanted_counterparties or not page_counterparties.intersection(wanted_counterparties)
    ):
        return False

    if mode == WikiRetrievalMode.EXTERNAL_DRAFT:
        return (
            visibility_explicit
            and _norm(visibility) in {"external_safe", "public"}
            and external_grounding
            and normalized_scope in {"global", "market"}
            and not sensitive_kinds
            and _norm(status) in {"active", "trusted"}
        )

    if mode == WikiRetrievalMode.PROJECT_CONTEXT:
        # Generic pages containing exact identifiers are not reusable project
        # knowledge. Matching project-scoped pages remain eligible.
        if sensitive_kinds and normalized_scope != "project":
            return False
        if normalized_scope == "example":
            return False
        return True

    if mode == WikiRetrievalMode.CURATOR:
        return True

    return True


def search_wiki(request: WikiSearchRequest) -> WikiSearchResult:
    """Execute one structured wiki search through the canonical index."""
    from src.wiki.indexer import format_wiki_hits, wiki_search_hits

    domain = request.domains[0] if request.domains else None
    hits = wiki_search_hits(
        request.query,
        top_k=request.top_k,
        include_graph_neighbors=request.include_graph_neighbors,
        domain_filter=domain,
        domain_mode="soft" if domain else "hard",
        expand=request.expand,
        request=request,
    )
    return WikiSearchResult(
        request=request,
        hits=hits,
        formatted=format_wiki_hits(hits, query=request.query),
    )
