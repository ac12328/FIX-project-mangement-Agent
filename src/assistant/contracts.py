"""Surface-neutral contracts for assistant application services."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field


class AssistantIntent(BaseModel):
    """Normalized intent shared by API adapters and assistant services."""

    model_config = ConfigDict(extra="ignore")

    type: Literal["status", "compare", "draft", "aggregate_query", "project_update"]
    project_ids: list[str] = Field(default_factory=list, max_length=25)
    prompt: str | None = Field(default=None, max_length=20_000)
    field: str | None = None
    value: Any = None
