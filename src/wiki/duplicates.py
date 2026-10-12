"""Explainable, high-precision duplicate candidate detection for wiki planning."""
from __future__ import annotations

import re
from dataclasses import dataclass

from src.wiki.corpus import CorpusPage

_GENERIC = {
    "the", "and", "for", "with", "from", "market", "markets", "knowledge",
    "requirements", "requirement", "procedure", "process", "information",
}
_ENTITY_TYPES = {"entity"}


@dataclass(frozen=True)
class DuplicateCandidate:
    left_path: str
    right_path: str
    left_hash: str
    right_hash: str
    title_score: float
    body_score: float
    source_score: float
    type_score: float
    scope_score: float
    total_score: float
    action: str
    rationale: tuple[str, ...]


@dataclass(frozen=True)
class ArchiveRedirectCheck:
    path: str
    content_hash: str
    target: str | None
    status: str
    issues: tuple[str, ...]


def _tokens(text: str) -> set[str]:
    return {
        token for token in re.findall(r"[a-z0-9]{3,}", (text or "").lower())
        if token not in _GENERIC
    }


def _jaccard(left: set[str], right: set[str]) -> float:
    if not left or not right:
        return 0.0
    return len(left & right) / len(left | right)


def _scope(page: CorpusPage) -> str:
    return page.page.governance.scope.value if page.page.governance else "legacy_unscoped"


def _is_active(page: CorpusPage) -> bool:
    return (page.page.status or "active").lower() != "archived"


def _is_market_boilerplate(left: CorpusPage, right: CorpusPage) -> bool:
    if not (left.path.startswith("markets/") and right.path.startswith("markets/")):
        return False
    return not (_tokens(left.page.title) & _tokens(right.page.title))


def compare_pages(left: CorpusPage, right: CorpusPage) -> DuplicateCandidate:
    """Classify a page pair without treating metadata overlap as merge proof."""
    title_score = _jaccard(_tokens(left.page.title), _tokens(right.page.title))
    body_score = _jaccard(_tokens(left.page.body), _tokens(right.page.body))
    source_score = _jaccard(set(left.page.sources), set(right.page.sources))
    type_score = 1.0 if left.page.type == right.page.type else 0.0
    scope_score = 1.0 if _scope(left) == _scope(right) else 0.0
    total = (
        title_score * 0.40
        + body_score * 0.40
        + source_score * 0.05
        + type_score * 0.10
        + scope_score * 0.05
    )

    rationale: list[str] = []
    if title_score >= 0.6:
        rationale.append("similar_title")
    if body_score >= 0.55:
        rationale.append("similar_body")
    if source_score > 0:
        rationale.append("shared_source_support")
    if type_score:
        rationale.append("same_type")

    if not _is_active(left) or not _is_active(right):
        action = "archive_check_only"
        rationale.append("archived_page_excluded")
    elif _is_market_boilerplate(left, right):
        action = "keep_separate"
        rationale.append("market_boilerplate")
    elif left.page.type in _ENTITY_TYPES or right.page.type in _ENTITY_TYPES:
        action = "manual_conflict_review" if title_score >= 0.8 and body_score >= 0.8 else "keep_separate"
        rationale.append("entity_auto_merge_blocked")
    elif left.page.type != right.page.type:
        action = "link_or_hub_review" if title_score >= 0.5 or body_score >= 0.6 else "keep_separate"
        rationale.append("type_mismatch")
    elif _scope(left) != _scope(right):
        action = "split_then_merge" if title_score >= 0.75 and body_score >= 0.75 else "keep_separate"
        rationale.append("scope_mismatch")
    elif left.sensitive_kinds or right.sensitive_kinds:
        action = "manual_conflict_review"
        rationale.append("sensitive_content")
    elif title_score >= 0.75 and body_score >= 0.75:
        action = "merge_into"
    elif title_score >= 0.55 and body_score >= 0.45:
        action = "manual_conflict_review"
    elif title_score >= 0.65 or body_score >= 0.75:
        action = "link_or_hub_review"
    else:
        action = "keep_separate"

    return DuplicateCandidate(
        left_path=left.path,
        right_path=right.path,
        left_hash=left.content_hash,
        right_hash=right.content_hash,
        title_score=round(title_score, 4),
        body_score=round(body_score, 4),
        source_score=round(source_score, 4),
        type_score=type_score,
        scope_score=scope_score,
        total_score=round(total, 4),
        action=action,
        rationale=tuple(rationale),
    )


def find_duplicate_candidates(
    pages: list[CorpusPage],
    *,
    minimum_score: float = 0.42,
) -> list[DuplicateCandidate]:
    """Return actionable active-page candidates; archived pages are audited separately."""
    active = [page for page in pages if _is_active(page)]
    candidates: list[DuplicateCandidate] = []
    for index, left in enumerate(active):
        for right in active[index + 1:]:
            title_overlap = _tokens(left.page.title) & _tokens(right.page.title)
            body_overlap = _tokens(left.page.body) & _tokens(right.page.body)
            if left.page.type != right.page.type and not title_overlap:
                continue
            if not title_overlap and len(body_overlap) < 3:
                continue
            candidate = compare_pages(left, right)
            if candidate.action != "keep_separate" and candidate.total_score >= minimum_score:
                candidates.append(candidate)
    return sorted(candidates, key=lambda item: (-item.total_score, item.left_path, item.right_path))


def audit_archive_redirects(pages: list[CorpusPage]) -> list[ArchiveRedirectCheck]:
    """Validate archived redirects independently from duplicate discovery."""
    by_path = {page.path: page for page in pages}
    checks: list[ArchiveRedirectCheck] = []
    for page in pages:
        if _is_active(page):
            continue
        governance = page.page.governance
        target = governance.superseded_by if governance else None
        if not target:
            derived = [link.target for link in page.page.links if link.rel == "derived_from"]
            target = derived[0] if len(derived) == 1 else None

        issues: list[str] = []
        if not target:
            if not (governance and governance.archive_reason):
                issues.append("missing_canonical_target")
        elif target == page.path:
            issues.append("self_reference")
        else:
            target_page = by_path.get(target)
            if target_page is None:
                issues.append("missing_target")
            elif not _is_active(target_page):
                issues.append("target_is_archived")
                seen = {page.path}
                cursor = target_page
                while cursor is not None and not _is_active(cursor):
                    if cursor.path in seen:
                        issues.append("redirect_cycle")
                        break
                    seen.add(cursor.path)
                    governance = cursor.page.governance
                    next_target = governance.superseded_by if governance else None
                    cursor = by_path.get(next_target) if next_target else None

        checks.append(ArchiveRedirectCheck(
            path=page.path,
            content_hash=page.content_hash,
            target=target,
            status="ok" if not issues else "invalid",
            issues=tuple(issues),
        ))
    return sorted(checks, key=lambda item: item.path)
