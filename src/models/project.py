"""
Project truth model (Pydantic).
Represents the canonical state of a project in the database.
"""

from datetime import datetime

from pydantic import BaseModel

from src.models.enums import BallWith, Milestone, ProjectStatus, ProjectTier


class Project(BaseModel):
    """Canonical project truth — mirrors the projects table."""
    project_id: str
    client_name: str | None = None
    client_email: str | None = None
    market: str | None = None
    project_status: ProjectStatus = ProjectStatus.ACTIVE
    ball_with: BallWith = BallWith.UNKNOWN
    current_milestone: Milestone | None = None
    broker_code: str | None = None
    connection_type: str | None = "FIX 4.2"
    target_go_live: str | None = None
    notes: str | None = None
    sla_limit: float | None = None
    logbook: str | None = None
    next_action: str | None = None
    operator_id: str = "default_operator"
    project_tier: ProjectTier | str | None = None
    work_item_id: str | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None
    last_activity_at: datetime | None = None
