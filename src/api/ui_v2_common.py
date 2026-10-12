"""Shared helpers for Dashboard UI API v2 read models."""

from __future__ import annotations

from collections import Counter
from datetime import date, datetime, timezone
from typing import Iterable

from src.api.auth import CurrentActor
from src.api.schemas.ui_v2 import FacetReadModel, FacetValue
from src.auth.rbac import P_BUSINESS_WRITE
from src.models.enums import ProjectStatus

def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def can_execute(actor: CurrentActor) -> bool:
    return P_BUSINESS_WRITE in actor.permissions


def _parse_datetime(value: str | None) -> datetime | None:
    if not value:
        return None
    normalized = value.strip().replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError:
        try:
            parsed = datetime.combine(date.fromisoformat(normalized[:10]), datetime.min.time())
        except ValueError:
            return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _age_days(value: str | None) -> int | None:
    parsed = _parse_datetime(value)
    if parsed is None:
        return None
    return max(0, (datetime.now(timezone.utc) - parsed).days)


def _project_health(project_status: str, ball_with: str, days_stale: int) -> str:
    # Preserve the three-color API contract; closed projects have no active risk.
    # Worklists render terminal status as neutral and exclude it from health filters.
    if project_status in ProjectStatus.terminal_statuses():
        return "green"
    if project_status == "Blocked" or days_stale >= 14:
        return "red"
    if project_status == "Waiting" or ball_with == "External" or days_stale >= 7:
        return "yellow"
    return "green"


def _facets(values: dict[str, tuple[str, Iterable[str]]]) -> list[FacetReadModel]:
    result: list[FacetReadModel] = []
    for facet_id, (label, raw_values) in values.items():
        counts = Counter(str(value) for value in raw_values if value not in (None, ""))
        result.append(
            FacetReadModel(
                id=facet_id,
                label=label,
                values=[
                    FacetValue(value=value, count=count)
                    for value, count in sorted(counts.items(), key=lambda item: (-item[1], item[0]))
                ],
            )
        )
    return result
