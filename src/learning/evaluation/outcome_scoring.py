"""Deterministic WP10 outcome evidence scoring contracts.

These scores describe persisted evidence only. Lifecycle application belongs to WP11.
"""
from __future__ import annotations

from pydantic import BaseModel, ConfigDict


class OutcomeScore(BaseModel):
    model_config = ConfigDict(frozen=True)
    outcome_type: str
    score: float
    reinforces: bool


def score_outcome(outcome_type: str, outcome_value: float) -> OutcomeScore:
    if outcome_type in {"positive", "send", "client_reply"} and outcome_value > 0:
        return OutcomeScore(outcome_type=outcome_type, score=min(1.0, outcome_value), reinforces=True)
    if outcome_type in {"negative", "correction"}:
        return OutcomeScore(outcome_type=outcome_type, score=max(-1.0, min(0.0, outcome_value)), reinforces=False)
    return OutcomeScore(outcome_type=outcome_type, score=0.0, reinforces=False)
