"""
Parse stall / next-action fields from wiki pm-problem-index.md (SSOT for rule 58).
"""

from __future__ import annotations

import re
from functools import lru_cache

from config.settings import BASE_DIR

_INDEX_PATH = (
    BASE_DIR / "wiki" / "playbook" / "fix-connectivity" / "pm-problem-index.md"
)

_MILESTONE_SECTION_RE = re.compile(r"^## (M[1-8]_[A-Za-z]+)\s*$", re.MULTILINE)
_FIELD_RES = {
    "recommended_next_action": re.compile(
        r"\*\*recommended_next_action:\*\*\s*(.+)", re.IGNORECASE
    ),
    "draft_signal": re.compile(
        r"\*\*draft_signal:\*\*\s*`?([^`\n]+)`?", re.IGNORECASE
    ),
    "escalation_after_days": re.compile(
        r"\*\*escalation_after_days:\*\*\s*(.+)", re.IGNORECASE
    ),
    "stall_policy": re.compile(r"\*\*Stall policy:\*\*\s*(.+)", re.IGNORECASE),
}


def clear_pm_problem_index_cache() -> None:
    loader = _load_index_text
    if hasattr(loader, "cache_clear"):
        loader.cache_clear()


@lru_cache(maxsize=1)
def _load_index_text() -> str:
    if not _INDEX_PATH.is_file():
        return ""
    return _INDEX_PATH.read_text(encoding="utf-8")


def _section_body(text: str, milestone: str) -> str:
    if not text:
        return ""
    parts = _MILESTONE_SECTION_RE.split(text)
    for i in range(1, len(parts), 2):
        if parts[i] == milestone:
            return parts[i + 1].strip() if i + 1 < len(parts) else ""
    return ""


def get_pm_stall_guidance(milestone: str) -> dict[str, str]:
    """
    Return stall-related fields for a milestone section (first matching block).

    Used by AdvisoryEngine when External + >3 business days inactive (rule 58).
    """
    body = _section_body(_load_index_text(), milestone)
    if not body:
        return {}

    blocks = re.split(r"\n(?=### )", body)
    for block in blocks:
        out: dict[str, str] = {}
        for key, pattern in _FIELD_RES.items():
            m = pattern.search(block)
            if m:
                out[key] = m.group(1).strip()
        if out:
            return out

    out: dict[str, str] = {}
    for key, pattern in _FIELD_RES.items():
        m = pattern.search(body)
        if m:
            out[key] = m.group(1).strip()
    return out


def primary_stall_action(guidance: dict[str, str]) -> str | None:
    if not guidance:
        return None
    return guidance.get("recommended_next_action") or guidance.get("stall_policy")