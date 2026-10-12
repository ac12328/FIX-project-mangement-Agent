"""Read-only portfolio reporting contracts; no inferred scheduling writes."""

from typing import Literal

from pydantic import BaseModel, Field


class ProjectReportItem(BaseModel):
    project_id: str
    client_name: str | None = None
    market: str | None = None
    project_status: str
    current_milestone: str | None = None
    ball_with: str
    operator_id: str
    tracked_since: str | None = None
    target_go_live: str | None = None
    next_action: str | None = None
    completed_at: str | None = None
    data_notes: list[str] = Field(default_factory=list)


class ProjectSummaryReport(BaseModel):
    generated_at: str
    report_date: str
    timezone: Literal["Asia/Hong_Kong"] = "Asia/Hong_Kong"
    completed_from: str
    completed_through: str
    scope: Literal["all", "mine"]
    active: list[ProjectReportItem]
    postponed: list[ProjectReportItem]
    completed: list[ProjectReportItem]
    warnings: list[str] = Field(default_factory=list)
