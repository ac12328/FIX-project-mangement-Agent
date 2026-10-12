"""
Shared scoring evaluators for morning brief and attention queue.
Single source of truth for operator_flag and project_tier weights.
"""
from __future__ import annotations

CANONICAL_FLAG_SCORES: dict[str, int] = {"urgent": 30, "watch": 15}
CANONICAL_TIER_SCORES: dict[str, int] = {"high": 15, "medium": 8, "low": 0}


def score_operator_flag(flag: str | None) -> int:
    return CANONICAL_FLAG_SCORES.get(flag or "", 0)


def score_project_tier(tier: str | None) -> int:
    return CANONICAL_TIER_SCORES.get(tier or "", 0)
