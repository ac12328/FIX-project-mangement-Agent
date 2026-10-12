"""Shared bounded action mutation and bundle contracts."""
import json
from datetime import date
from typing import Annotated, Literal
from pydantic import BaseModel, ConfigDict, Field, model_validator

class ActionMutation(BaseModel):
    model_config=ConfigDict(extra="forbid")
    action_id: str = Field(min_length=1,max_length=200)
    operation: Literal["create","update","complete","cancel"]
    expected_revision: int | None = Field(default=None,ge=0)
    description: str | None = Field(default=None,min_length=1,max_length=2000)
    owner_side: Literal["Internal","External","Shared"] | None = None
    due_date: date | None = None
    reminder_due_at: date | None = None
    depends_on: list[Annotated[str, Field(min_length=1, max_length=200)]] | None = Field(default=None,max_length=50)

    @model_validator(mode="after")
    def valid_operation(self):
        if self.operation=="create":
            if not self.action_id.startswith("new:") or not self.description or not self.description.strip():
                raise ValueError("create requires a new: reference and description")
        elif self.expected_revision is None:
            raise ValueError("existing action requires expected_revision")
        if self.operation in {"complete","cancel"} and self.model_fields_set.intersection({"description","owner_side","due_date","reminder_due_at","depends_on"}):
            raise ValueError("terminal action update must not contain unrelated field changes")
        if self.operation=="update" and not self.model_fields_set.intersection({"description","owner_side","due_date","reminder_due_at","depends_on"}):
            raise ValueError("empty action update")
        return self

class ActionBundle(BaseModel):
    model_config=ConfigDict(extra="forbid")
    operation_id: str = Field(min_length=1,max_length=120)
    expected_plan_revision: int = Field(ge=0)
    expected_project_version: str
    reason: str = Field(min_length=1,max_length=2000)
    evidence_ref: str | None = Field(default=None, max_length=500)
    replace_legacy_summary: bool = False
    actions: list[ActionMutation] = Field(min_length=1,max_length=50)


    def identity_json(self) -> str:
        """Preserve mutation field presence: omitted and explicit null differ."""
        value = self.model_dump(mode="json")
        value["actions"] = [action.model_dump(mode="json", exclude_unset=True) for action in self.actions]
        return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
