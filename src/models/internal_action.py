from typing import Optional

from pydantic import BaseModel, ConfigDict

from src.models.enums import InternalActionStatus, OwnerTeam

# Single source: derived from OwnerTeam enum (matches DB CHECK).
OWNER_TEAMS = tuple(t.value for t in OwnerTeam)


class InternalAction(BaseModel):
    model_config = ConfigDict(use_enum_values=True)

    action_id: str
    project_id: Optional[str] = None
    owner_team: OwnerTeam | str
    title: str
    deadline: Optional[str] = None  # ISO 8601 date e.g. "2026-06-01"
    status: InternalActionStatus | str = InternalActionStatus.OPEN
    created_at: Optional[str] = None
    completed_at: Optional[str] = None
