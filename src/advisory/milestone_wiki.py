"""
Load milestone PM guidance from wiki playbooks — single source with project_advisor.md.

Replaces MILESTONE_KNOWLEDGE hardcoded dict in fix_onboarding.py.
"""

from __future__ import annotations

import re
from functools import lru_cache
from pathlib import Path

from config.settings import BASE_DIR

_GUIDE_PATH = BASE_DIR / "wiki" / "playbook" / "fix-connectivity" / "fix-onboarding-milestone-pm-guide.md"
_HANDOFF_PATH = BASE_DIR / "wiki" / "playbook" / "b2b" / "fix-production-session-launch-handoff.md"
_CRITERIA_PATH = BASE_DIR / "wiki" / "playbook" / "fix-connectivity" / "milestone-completion-criteria.md"

_SECTION_RE = re.compile(r"^## (M[1-8]_[A-Za-z]+)\s*$", re.MULTILINE)
_FIELD_RE = {
    "typical_ball": re.compile(r"\*\*Typical ball:\*\*\s*(.+)", re.IGNORECASE),
    "typical_duration_days": re.compile(r"\*\*Typical duration:\*\*\s*(\d+)", re.IGNORECASE),
    "typical_blockers": re.compile(r"\*\*Typical blockers:\*\*\s*(.+)", re.IGNORECASE),
    "key_actions": re.compile(r"\*\*Key actions:\*\*\s*(.+)", re.IGNORECASE),
}
# Evidence requirement line in milestone-completion-criteria.md sections.
_EVIDENCE_RE = re.compile(r"\*\*Evidence:\*\*\s*(.+)", re.IGNORECASE)


def clear_milestone_wiki_cache() -> None:
    """Clear cached wiki text (for tests)."""
    _load_guide_text.cache_clear()
    _load_handoff_text.cache_clear()
    _load_criteria_text.cache_clear()


@lru_cache(maxsize=1)
def _load_guide_text() -> str:
    if not _GUIDE_PATH.is_file():
        return ""
    return _GUIDE_PATH.read_text(encoding="utf-8")


@lru_cache(maxsize=1)
def _load_handoff_text() -> str:
    if not _HANDOFF_PATH.is_file():
        return ""
    return _HANDOFF_PATH.read_text(encoding="utf-8")


@lru_cache(maxsize=1)
def _load_criteria_text() -> str:
    if not _CRITERIA_PATH.is_file():
        return ""
    return _CRITERIA_PATH.read_text(encoding="utf-8")


def _section_body(text: str, milestone: str) -> str:
    if not text:
        return ""
    parts = _SECTION_RE.split(text)
    for i in range(1, len(parts), 2):
        if parts[i] == milestone:
            return parts[i + 1].strip() if i + 1 < len(parts) else ""
    return ""


def get_milestone_guide(milestone: str) -> dict:
    """Parse milestone section from wiki PM guide into a knowledge dict."""
    body = _section_body(_load_guide_text(), milestone)
    if not body:
        return {}

    guide: dict = {}
    for field, pattern in _FIELD_RE.items():
        m = pattern.search(body)
        if not m:
            continue
        raw = m.group(1).strip()
        if field == "typical_duration_days":
            guide[field] = int(raw)
        elif field == "typical_blockers":
            guide[field] = [b.strip() for b in raw.split(";")]
        elif field == "key_actions":
            guide[field] = [a.strip() for a in raw.split(";")]
        elif field == "typical_ball":
            guide["ball_usually_with"] = raw

    return guide


def get_milestone_completion_criteria(milestone: str) -> list[str]:
    """Return only authoritative versioned operational criteria."""
    from src.milestone.contracts import get_milestone_contract

    contract = get_milestone_contract(milestone)
    contract_criteria = contract.required_exit_criteria or contract.close_criteria
    return [f"{item.id}: {item.description}" for item in contract_criteria]


def get_milestone_wiki_supplements(milestone: str) -> list[str]:
    """Return optional human-readable wiki context; never use as a gate."""
    body = _section_body(_load_criteria_text(), milestone)
    return [
        text for match in _EVIDENCE_RE.finditer(body)
        if (text := match.group(1).strip().rstrip(".").strip())
    ]


def get_milestone_wiki_excerpt(milestone: str, *, max_chars: int = 3000) -> str:
    """Return wiki text for harness injection."""
    guide_body = _section_body(_load_guide_text(), milestone)
    if not guide_body:
        return ""
    excerpt = f"## {milestone}\n{guide_body}"
    if milestone.startswith("M8"):
        handoff = _load_handoff_text()
        if handoff:
            excerpt += "\n\n" + handoff[:600]
    return excerpt[:max_chars]


def search_milestone_context(milestone: str, market: str | None = None) -> str:
    """Wiki search supplement — falls back to parsed guide section."""
    query = f"{milestone} FIX onboarding PM"
    if market:
        query += f" {market}"
    try:
        from src.wiki.retrieval import WikiSearchRequest, search_wiki
        results = search_wiki(WikiSearchRequest(
            query=query,
            market=market,
            top_k=2,
            include_graph_neighbors=True,
        )).formatted
        if results and results.strip():
            return results[:3000]
    except Exception:
        pass
    return get_milestone_wiki_excerpt(milestone)