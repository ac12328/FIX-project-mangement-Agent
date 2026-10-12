"""Deterministic ranking and preference override for canonical memory facts."""
from __future__ import annotations

import re
from datetime import datetime
from typing import Protocol

from pydantic import BaseModel, ConfigDict, field_validator

_SCOPE_RANK = {"contact": 6, "project": 5, "counterparty": 4, "operator": 3, "market": 2, "organization": 1, "global": 0}
_TRUST_RANK = {"operator": 4, "verified": 3, "internal": 2, "external": 1, "unknown": 0}
_WORD = re.compile(r"[a-z0-9_]+")


class RankableFact(Protocol):
    fact_id: str
    target_type: str
    subject_type: str
    subject_id: str | None
    predicate: str
    value: object
    scope_type: str
    source_type: str
    trust_tier: str
    confidence: float
    evidence_count: int
    valid_from: datetime
    attributed_outcome_score: float
    conflict_penalty: float


class RankingContext(BaseModel):
    model_config = ConfigDict(frozen=True)
    query: str = ""
    at: datetime

    @field_validator("at")
    @classmethod
    def _aware_utc(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None or value.utcoffset().total_seconds() != 0:
            raise ValueError("at must be aware UTC")
        return value


def estimate_tokens(text: str) -> int:
    """Conservative deterministic token estimate used for packing."""
    return max(1, (len(text.encode("utf-8")) + 2) // 3)


def _semantic_key(fact: RankableFact) -> tuple[str, str | None, str]:
    return fact.subject_type, fact.subject_id, fact.predicate


def _preference_override(facts: list[RankableFact]) -> list[RankableFact]:
    winners: dict[tuple[str, str | None, str], RankableFact] = {}
    preserved: list[RankableFact] = []
    for fact in facts:
        if fact.target_type != "preference":
            preserved.append(fact)
            continue
        key = _semantic_key(fact)
        current = winners.get(key)
        candidate = (_SCOPE_RANK.get(fact.scope_type, -1), fact.confidence, fact.fact_id)
        existing = (_SCOPE_RANK.get(current.scope_type, -1), current.confidence, current.fact_id) if current else None
        if existing is None or candidate > existing:
            winners[key] = fact
    return preserved + list(winners.values())


def _score(fact: RankableFact, context: RankingContext) -> tuple:
    query_terms = set(_WORD.findall(context.query.lower()))
    haystack = f"{fact.subject_type} {fact.predicate} {fact.value}".lower()
    relevance = len(query_terms.intersection(_WORD.findall(haystack)))
    age_days = max(0, (context.at - fact.valid_from).days)
    freshness = max(0, 3650 - age_days)
    token_cost = estimate_tokens(str(fact.value))
    return (
        _SCOPE_RANK.get(fact.scope_type, -1),
        relevance,
        _TRUST_RANK.get(fact.trust_tier, 0),
        round(fact.attributed_outcome_score, 6),
        -round(fact.conflict_penalty, 6),
        round(fact.confidence, 6),
        fact.evidence_count,
        freshness,
        -token_cost,
        fact.fact_id,
    )


def rank_facts(facts: list[RankableFact], context: RankingContext) -> list[RankableFact]:
    """Apply learned-preference override, then stable deterministic ranking."""
    return sorted(_preference_override(facts), key=lambda fact: _score(fact, context), reverse=True)
