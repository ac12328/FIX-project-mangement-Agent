"""Pydantic models for LLM analytics endpoints."""

from pydantic import BaseModel


class LLMSummary(BaseModel):
    total_calls: int
    total_tokens: int
    avg_latency_ms: float
    error_count: int
    error_rate_pct: float
    period_days: int


class TaskBreakdown(BaseModel):
    task: str
    count: int
    avg_tokens: float
    avg_latency_ms: float
    error_count: int


class TaskBreakdownResponse(BaseModel):
    tasks: list[TaskBreakdown]


class DailyTrend(BaseModel):
    date: str
    calls: int
    tokens: int
    errors: int
    avg_latency_ms: float


class TrendsResponse(BaseModel):
    days: list[DailyTrend]
