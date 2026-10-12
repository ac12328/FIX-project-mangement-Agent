"""Deterministic baseline-versus-candidate quality scoring."""
from __future__ import annotations

from pydantic import BaseModel, ConfigDict


class QualityPolicy(BaseModel):
    """Candidate quality floor and maximum permitted baseline regression."""

    model_config = ConfigDict(frozen=True)
    min_candidate_quality: float = 0.5
    max_quality_regression: float = 0.0


class QualityScore(BaseModel):
    """Typed per-case baseline/candidate quality comparison."""

    model_config = ConfigDict(frozen=True)
    baseline: float
    candidate: float
    delta: float
    passed: bool


def score_case(baseline: float, candidate: float, policy: QualityPolicy) -> QualityScore:
    """Pass only when the candidate meets quality and regression thresholds."""
    delta = candidate - baseline
    passed = candidate >= policy.min_candidate_quality and delta >= -policy.max_quality_regression
    return QualityScore(baseline=baseline, candidate=candidate, delta=delta, passed=passed)


def quality(response, case) -> float:
    """Score expected keyword coverage in a typed replay response."""
    text = response.text.lower()
    matched = sum(keyword.lower() in text for keyword in case.expected_keywords)
    return matched / len(case.expected_keywords)
