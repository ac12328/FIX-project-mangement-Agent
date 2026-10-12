"""
Canonical project reference resolution for the agent harness.

Handles configured portfolio IDs, legacy hyphen IDs (PROJ-001), loose tokens
(me 2026), and ambiguity when multiple candidates share the same year.
"""

from __future__ import annotations

import re
import sqlite3
from dataclasses import dataclass, field
from difflib import SequenceMatcher

PORTFOLIO_ID_RE = re.compile(r"\b([A-Za-z]{2}\d{4})\b")
LEGACY_ID_RE = re.compile(r"\b([A-Z][A-Z0-9_]{1,}-\d{2,})\b", re.IGNORECASE)
LOOSE_ID_RE = re.compile(r"\b([a-zA-Z]{2})\s*(\d{4})\b")
PROJECT_LIKE_TOKEN_RE = re.compile(r"\b([A-Za-z]{2}\d{4,5})\b")
CONFORMANCE_CASE_RE = re.compile(r"\b[ATS]\.?\d{3}\b", re.IGNORECASE)
_CRITERION_ID_RE = re.compile(r"\bM[1-8]-(?:EXIT|ENTRY|OPT)-\d+\b", re.IGNORECASE)
_CRITERION_TAIL_RE = re.compile(r"^(?:EXIT|ENTRY|OPT)-\d+$", re.IGNORECASE)

# Pairs of two-letter prefixes that are easily confused in speech/typing.
_CONFUSABLE_PREFIX_PAIRS = frozenset({
    frozenset({"ME", "MM"}),
})


@dataclass
class ProjectResolveResult:
    project_id: str | None = None
    ambiguous: bool = False
    candidates: list[str] = field(default_factory=list)
    source: str = ""


def _normalize_known_ids(known_ids: list[str] | None) -> list[str]:
    if not known_ids:
        return []
    return sorted({pid.upper() for pid in known_ids if pid})


def _ids_for_year(known_ids: list[str], year: str) -> list[str]:
    suffix = year if len(year) == 4 else year
    return [pid for pid in known_ids if pid.endswith(suffix)]


def _prefixes_confusable(a: str, b: str) -> bool:
    pair = frozenset({a.upper(), b.upper()})
    return pair in _CONFUSABLE_PREFIX_PAIRS


def _edit_distance_at_most_one(left: str, right: str) -> bool:
    """Return True when two identifiers differ by one edit at most."""
    if abs(len(left) - len(right)) > 1:
        return False
    if len(left) == len(right):
        return sum(a != b for a, b in zip(left, right)) <= 1
    shorter, longer = (left, right) if len(left) < len(right) else (right, left)
    index_short = 0
    index_long = 0
    skipped = False
    while index_short < len(shorter) and index_long < len(longer):
        if shorter[index_short] == longer[index_long]:
            index_short += 1
            index_long += 1
            continue
        if skipped:
            return False
        skipped = True
        index_long += 1
    return True


def _typo_candidates(token: str, known_ids: list[str]) -> list[str]:
    normalized = token.upper()
    prefix = normalized[:2]
    candidates = [
        pid for pid in known_ids
        if pid[:2] == prefix and _edit_distance_at_most_one(normalized, pid)
    ]
    return sorted(
        candidates,
        key=lambda pid: SequenceMatcher(None, normalized, pid).ratio(),
        reverse=True,
    )


def looks_like_milestone_criterion_id(token: str) -> bool:
    """True for contract IDs such as M6-EXIT-01, or the EXIT-01 tail they contain."""
    raw = str(token or "").strip()
    if not raw:
        return False
    return bool(_CRITERION_ID_RE.fullmatch(raw) or _CRITERION_TAIL_RE.fullmatch(raw))


def has_project_like_reference(text: str) -> bool:
    """Return True for exact or malformed explicit portfolio-ID tokens."""
    if not text:
        return False
    if PROJECT_LIKE_TOKEN_RE.search(text) or LOOSE_ID_RE.search(text):
        return True
    return any(
        not looks_like_milestone_criterion_id(match.group(1))
        for match in LEGACY_ID_RE.finditer(text)
    )


def looks_like_conformance_case_id(token: str) -> bool:
    """True for FIX case IDs such as S002, A001, T011, or A.001."""
    return bool(re.fullmatch(r"[ATS]\.?\d{3}", str(token or "").strip(), re.I))


def is_conformance_case_lookup(text: str) -> bool:
    """True when the text names a conformance case and no portfolio project ID."""
    if not text or not CONFORMANCE_CASE_RE.search(text):
        return False
    return not has_project_like_reference(text)


def _ambiguous_loose_match(
    prefix: str,
    year: str,
    known_ids: list[str],
) -> ProjectResolveResult | None:
    """Return ambiguity result when a loose token matches multiple known IDs."""
    year_ids = _ids_for_year(known_ids, year)
    if not year_ids:
        return None

    normalized = f"{prefix.upper()}{year}"
    exact = [pid for pid in year_ids if pid == normalized]
    # Operator typed an explicit prefix (case-insensitive spelling) — trust it when in DB.
    if len(exact) == 1:
        return ProjectResolveResult(
            project_id=exact[0],
            source="normalized",
        )

    confusable = [
        pid for pid in year_ids
        if _prefixes_confusable(prefix, pid[:2])
    ]
    if len(confusable) >= 2:
        return ProjectResolveResult(
            ambiguous=True,
            candidates=sorted(confusable),
            source="normalized",
        )

    if len(year_ids) >= 2:
        return ProjectResolveResult(
            ambiguous=True,
            candidates=sorted(year_ids),
            source="normalized",
        )

    return None


def resolve_project_reference(
    text: str,
    *,
    known_ids: list[str] | None = None,
) -> ProjectResolveResult:
    """Resolve a project ID from operator text."""
    if not text or not text.strip():
        return ProjectResolveResult()

    known = _normalize_known_ids(known_ids)
    upper = text.upper()

    project_like_match = PROJECT_LIKE_TOKEN_RE.search(text)
    if project_like_match:
        pid = project_like_match.group(1).upper()
        if len(pid) == 6:
            if known and pid not in known:
                candidates = _typo_candidates(pid, known)
                if len(candidates) == 1:
                    return ProjectResolveResult(
                        project_id=candidates[0], source="typo_corrected",
                    )
                if len(candidates) > 1:
                    return ProjectResolveResult(
                        ambiguous=True,
                        candidates=candidates,
                        source="typo_corrected",
                    )
                return ProjectResolveResult(source="unresolved_explicit")
            return ProjectResolveResult(project_id=pid, source="explicit")

        if known:
            candidates = _typo_candidates(pid, known)
            if len(candidates) == 1:
                return ProjectResolveResult(
                    project_id=candidates[0], source="typo_corrected",
                )
            if len(candidates) > 1:
                return ProjectResolveResult(
                    ambiguous=True,
                    candidates=candidates,
                    source="typo_corrected",
                )
        return ProjectResolveResult(source="unresolved_explicit")

    for legacy_match in LEGACY_ID_RE.finditer(text):
        pid = legacy_match.group(1).upper()
        if looks_like_milestone_criterion_id(pid):
            continue
        if known:
            for kid in known:
                if kid.upper() == pid:
                    return ProjectResolveResult(project_id=kid, source="explicit")
            if pid not in known:
                return ProjectResolveResult(source="explicit")
        return ProjectResolveResult(project_id=pid, source="explicit")

    loose_match = LOOSE_ID_RE.search(text)
    if loose_match:
        prefix, year = loose_match.group(1), loose_match.group(2)
        if known:
            amb = _ambiguous_loose_match(prefix, year, known)
            if amb is not None:
                return amb
        normalized = f"{prefix.upper()}{year}"
        return ProjectResolveResult(project_id=normalized, source="normalized")

    if known:
        hits = [pid for pid in known if pid in upper]
        if len(hits) == 1:
            return ProjectResolveResult(project_id=hits[0], source="substring")
        if len(hits) > 1:
            return ProjectResolveResult(
                ambiguous=True,
                candidates=sorted(hits),
                source="substring",
            )

    return ProjectResolveResult()


def list_known_project_ids(conn: sqlite3.Connection) -> list[str]:
    rows = conn.execute("SELECT project_id FROM projects").fetchall()
    return [dict(row)["project_id"] for row in rows]


def resolve_project_from_db(
    conn: sqlite3.Connection | None,
    text: str,
) -> ProjectResolveResult:
    known = list_known_project_ids(conn) if conn is not None else []
    return resolve_project_reference(text, known_ids=known)


def resolved_project_id(
    conn: sqlite3.Connection | None,
    text: str,
) -> str | None:
    """Return a single project ID, or None if absent or ambiguous."""
    result = resolve_project_from_db(conn, text)
    if result.ambiguous or not result.project_id:
        return None
    return result.project_id