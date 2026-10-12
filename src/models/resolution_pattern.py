"""
Resolution pattern model — learned from operator handling of support issues.
"""

from datetime import datetime
from typing import Literal

from pydantic import BaseModel


class ResolutionPattern(BaseModel):
    """A reusable resolution blueprint learned from operator decisions."""
    pattern_id: str
    category: str | None = None
    subcategory: str | None = None
    trigger_keywords: list[str] = []
    diagnosis_steps: list[str] = []
    resolution_action: str | None = None
    escalation_target: str | None = None
    typical_draft_template: str | None = None
    source: Literal["operator", "auto_extracted"] = "operator"
    times_used: int = 1
    times_confirmed: int = 0
    confidence: float = 0.5
    last_used_at: datetime | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None
