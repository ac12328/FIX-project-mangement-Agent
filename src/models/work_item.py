"""WorkItem Pydantic model — supertype envelope for all work items."""

from __future__ import annotations

from typing import Optional

from pydantic import BaseModel, ConfigDict

from src.models.enums import WorkItemType, WorkItemStatus, WorkItemPriority


class WorkItem(BaseModel):
    model_config = ConfigDict(extra="ignore", use_enum_values=True)

    work_item_id: str
    work_item_type: WorkItemType | str
    title: str
    status: WorkItemStatus | str
    priority: WorkItemPriority | str = WorkItemPriority.MEDIUM
    entity_table: Optional[str] = None   # 'projects' | 'support_tickets' | None
    entity_id: Optional[str] = None      # PK in entity_table
    created_at: Optional[str] = None
    updated_at: Optional[str] = None
