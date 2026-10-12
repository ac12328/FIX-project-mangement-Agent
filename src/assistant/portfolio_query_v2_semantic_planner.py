"""Deterministic semantic planning for high-confidence portfolio transition reads.

The parser maps compositional query concepts into the typed portfolio contract.
It never reads business data, supplies answers, or matches whole question templates.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from typing import Any, Literal

from src.assistant.portfolio_query_v2 import PortfolioQueryV2Request
from src.models.enums import ProjectStatus

DETERMINISTIC_SEMANTIC_PLANNER_VERSION = "deterministic-semantic-v1"

_LANGUAGE_ZH_RE = re.compile(r"[\u3400-\u9fff]")
_PROJECT_RE = re.compile(r"\bprojects?\b|\bportfolio\b|專案|項目|项目", re.IGNORECASE)
_TRANSITION_RE = re.compile(
    r"\b(?:became|become|changed|changes|changing|moved|moves|transitioned|transitions|"
    r"switched|switches|marked|set)\s+(?:to|as|into)\b|"
    r"(?:轉為|转为|轉成|转成|變成|变成|改為|改为|進入|进入)",
    re.IGNORECASE,
)
_DATE_TOKEN_RE = re.compile(r"(?<!\d)(\d{4}-\d{2}-\d{2})(?!\d)")

_STATUS_ALIASES: tuple[tuple[ProjectStatus, tuple[str, ...]], ...] = (
    (ProjectStatus.COMPLETED, (r"\bcompleted\b", r"\bcomplete\b", r"\bfinished\b", r"\bdone\b", "已完成", "完成咗", "完成了", "完成")),
    (ProjectStatus.ARCHIVED, (r"\barchived\b", r"\barchive\b", "已封存", "封存", "歸檔", "归档")),
    (ProjectStatus.CANCELLED, (r"\bcancelled\b", r"\bcanceled\b", r"\bcancel\w*\b", "已取消", "取消")),
    (ProjectStatus.ACTIVE, (r"\bactive\b", "進行中", "进行中", "活躍", "活跃")),
    (ProjectStatus.BLOCKED, (r"\bblocked\b", r"\bblocking\b", "受阻", "阻塞")),
    (ProjectStatus.WAITING, (r"\bwaiting\b", r"\bwait(?:ing)?\b", "等待中", "等待")),
)


@dataclass(frozen=True)
class SemanticPortfolioPlan:
    """Validated typed query plus privacy-safe deterministic planner metadata."""

    query: PortfolioQueryV2Request
    language: Literal["en", "zh-Hant"]
    reason: str

    def plan_payload(self) -> dict[str, Any]:
        return {
            "answer_mode": "typed",
            "query": self.query.model_dump(mode="json"),
            "language": self.language,
            "reason": self.reason,
        }

    def metadata(self) -> dict[str, Any]:
        return {
            "elapsed_seconds": 0.0,
            "estimated_input_tokens": 0,
            "estimated_output_tokens": 0,
            "model": DETERMINISTIC_SEMANTIC_PLANNER_VERSION,
            "planner_version": DETERMINISTIC_SEMANTIC_PLANNER_VERSION,
            "planner_source": "deterministic_semantic",
        }


def plan_portfolio_query_v2_semantically(
    question: str,
    *,
    as_of: datetime,
) -> SemanticPortfolioPlan | None:
    """Return a typed plan only when status-transition semantics are unambiguous."""
    text = " ".join(str(question or "").strip().split())
    if not text or not _PROJECT_RE.search(text):
        return None

    current = _normalize_as_of(as_of)
    date_range = _extract_date_range(text, current.date())
    if date_range is None:
        # A current-state status question must remain distinct from transition history.
        return None

    status = _extract_status(text)
    if status is None or not _has_transition_semantics(text, status):
        return None

    operation, group_by = _extract_operation(text)
    fields: list[str] = []
    sort: list[dict[str, str]] = []
    if operation == "list":
        fields = ["project_id", "client_name", "project_status", "transitioned_at"]
        sort = [{"field": "transitioned_at", "direction": "desc"}]

    query = PortfolioQueryV2Request.model_validate(
        {
            "operation": operation,
            "filters": {
                "transition": {
                    "field": "project_status",
                    "to_values": [status.value],
                    "effective_from": date_range[0].isoformat(),
                    "effective_to": date_range[1].isoformat(),
                }
            },
            "include_terminal": False,
            "group_by": group_by,
            "fields": fields,
            "sort": sort,
            "limit": 50,
            "cursor": None,
        }
    )
    language: Literal["en", "zh-Hant"] = "zh-Hant" if _LANGUAGE_ZH_RE.search(text) else "en"
    return SemanticPortfolioPlan(
        query=query,
        language=language,
        reason="Deterministic audit-backed project status-transition query.",
    )


def _normalize_as_of(value: datetime) -> datetime:
    current = value
    if current.tzinfo is None:
        current = current.replace(tzinfo=timezone.utc)
    return current.astimezone(timezone.utc)


def _extract_status(text: str) -> ProjectStatus | None:
    matched: list[ProjectStatus] = []
    for status, aliases in _STATUS_ALIASES:
        if any(re.search(alias, text, re.IGNORECASE) for alias in aliases):
            matched.append(status)
    unique = list(dict.fromkeys(matched))
    return unique[0] if len(unique) == 1 else None


def _has_transition_semantics(text: str, status: ProjectStatus) -> bool:
    if _TRANSITION_RE.search(text):
        return True
    aliases = dict(_STATUS_ALIASES)[status]
    if not any(re.search(alias, text, re.IGNORECASE) for alias in aliases):
        return False
    # A status verb combined with an explicit historical date range denotes the
    # transition event. Without a date range this parser never claims history.
    historical_markers = re.compile(
        r"\b(?:within|during|over|in|on|between|from|last|past|previous|recent|today|yesterday|this\s+(?:week|month|year)|year\s+to\s+date|last\s+(?:week|month))\b|"
        r"(?:過去|过去|最近|近|呢|這|这|今日|今天|昨日|昨天|本週|本周|上週|上周|本月|今年|本年|上月|由.+(?:至|到)|於|于)",
        re.IGNORECASE,
    )
    return historical_markers.search(text) is not None


def _extract_operation(text: str) -> tuple[Literal["count", "list", "exists", "group"], str | None]:
    if re.search(r"\b(?:group(?:ed)?|breakdown)\s+by\s+(?:current\s+)?status\b|按(?:目前)?(?:狀態|状态)(?:分組|分组|分類|分类)", text, re.IGNORECASE):
        return "group", "project_status"
    if re.search(r"\bhow\s+many\b|\bcount\b|\bnumber\s+of\b|幾多|几多|多少|數量|数量", text, re.IGNORECASE):
        return "count", None
    if re.search(r"\b(?:are|is|were|was)\s+there\s+any\b|\bdo\s+any\b|\bany\s+projects?\b|有冇|有無|有无|有沒有|有没有|是否有", text, re.IGNORECASE):
        return "exists", None
    return "list", None


def _extract_date_range(text: str, current_date: date) -> tuple[date, date] | None:
    explicit = _extract_explicit_date_range(text)
    if explicit is not None:
        return explicit

    relative_days = _extract_relative_days(text)
    if relative_days is not None:
        return current_date - timedelta(days=relative_days - 1), current_date

    lowered = text.casefold()
    if re.search(r"\btoday\b|今日|今天", lowered):
        return current_date, current_date
    if re.search(r"\byesterday\b|昨日|昨天", lowered):
        yesterday = current_date - timedelta(days=1)
        return yesterday, yesterday
    if re.search(r"\bthis\s+week\b|本週|本周", lowered):
        return current_date - timedelta(days=current_date.weekday()), current_date
    if re.search(r"\blast\s+week\b|上週|上周", lowered):
        this_week = current_date - timedelta(days=current_date.weekday())
        return this_week - timedelta(days=7), this_week - timedelta(days=1)
    if re.search(r"\b(?:this\s+year|year\s+to\s+date|ytd)\b|今年|本年", lowered):
        return current_date.replace(month=1, day=1), current_date
    if re.search(r"\bthis\s+month\b|本月", lowered):
        return current_date.replace(day=1), current_date
    if re.search(r"\blast\s+month\b|上月", lowered):
        month_start = current_date.replace(day=1)
        prior_end = month_start - timedelta(days=1)
        return prior_end.replace(day=1), prior_end
    return None


def _extract_explicit_date_range(text: str) -> tuple[date, date] | None:
    tokens = _DATE_TOKEN_RE.findall(text)
    if len(tokens) >= 2 and re.search(r"\b(?:between|from)\b|(?:由|從|从).*(?:至|到)|\d{4}-\d{2}-\d{2}\s*(?:至|到)", text, re.IGNORECASE):
        start = date.fromisoformat(tokens[0])
        end = date.fromisoformat(tokens[1])
        return (start, end) if start <= end else None
    if len(tokens) == 1 and re.search(r"\b(?:on|at)\b|(?:於|于)", text, re.IGNORECASE):
        selected = date.fromisoformat(tokens[0])
        return selected, selected
    return None


def _extract_relative_days(text: str) -> int | None:
    patterns = (
        r"\b(?:within|during|over|in|for)?\s*(?:the\s+)?(?:last|past|previous|recent|these|this)?\s*(\d{1,3})\s*days?\b",
        r"(?:過去|过去|最近|近|呢|這|这)?\s*(\d{1,3})\s*(?:日|天)(?:之?內|之?内)?",
    )
    for pattern in patterns:
        match = re.search(pattern, text, re.IGNORECASE)
        if match:
            days = int(match.group(1))
            if 1 <= days <= 366:
                return days
    return None


__all__ = [
    "DETERMINISTIC_SEMANTIC_PLANNER_VERSION",
    "SemanticPortfolioPlan",
    "plan_portfolio_query_v2_semantically",
]
