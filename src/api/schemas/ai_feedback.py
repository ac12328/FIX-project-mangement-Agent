"""AI feedback request/response models."""

from typing import Literal

from pydantic import BaseModel, Field, model_validator


class FeedbackRequest(BaseModel):
    item_type: str = Field(min_length=1, max_length=50)
    item_id: str = Field(min_length=1, max_length=200)
    project_id: str | None = Field(default=None, max_length=100)
    feedback_kind: Literal["correct", "wrong", "edited"]
    category: str | None = Field(default=None, max_length=100)
    operator_note: str | None = Field(default=None, max_length=2000)
    operator_id: str | None = Field(default=None, max_length=100)
    surface: Literal["chat", "web", "draft", "advisory", "tool"] | None = None
    trace_id: str | None = Field(default=None, max_length=100)
    run_id: str | None = Field(default=None, max_length=100)
    turn_id: str | None = Field(default=None, max_length=100)
    output_revision_id: str | None = Field(default=None, max_length=100)
    fact_id: str | None = Field(default=None, max_length=100)
    fact_version: int | None = Field(default=None, ge=1)
    feedback_scope: Literal["all_rendered", "style", "unrelated"] | None = None

    @model_validator(mode="after")
    def exact_selector_complete(self):
        if (self.fact_id is None) != (self.fact_version is None):
            raise ValueError("fact_id and fact_version must be supplied together")
        return self


class FeedbackResponse(BaseModel):
    success: bool
    feedback_id: str
    attribution_status: str | None = None
    outcome_ids: list[str] = Field(default_factory=list)


class FeedbackSummaryResponse(BaseModel):
    total: int
    correct: int
    wrong: int
    edited: int
    accuracy_pct: float
    top_error_categories: list[dict]


class FeedbackListResponse(BaseModel):
    items: list[dict]
