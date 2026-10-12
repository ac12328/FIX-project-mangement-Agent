"""API contracts for the AI Workspace surface."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from src.assistant.contracts import AssistantIntent


class AssistantCommandCapability(BaseModel):
    name: str = Field(min_length=1)
    usage: str = Field(min_length=1)
    description: str = Field(min_length=1)


class AssistantCapabilitiesResponse(BaseModel):
    enabled: bool
    web_search_available: bool
    web_search_reason: str | None
    web_search_provider: str | None
    commands: list[AssistantCommandCapability]
    part_types: list[str]
    can_create_actions: bool
    operator_writes: bool


class AssistantThreadResponse(BaseModel):
    thread_id: str
    owner_user_id: str
    title: str
    scope_mode: Literal["portfolio", "projects"]
    project_ids: list[str]
    web_enabled: bool
    archived_at: str | None
    created_at: str
    updated_at: str
    last_message: str | None = None


class AssistantThreadPage(BaseModel):
    items: list[AssistantThreadResponse]
    next_cursor: str | None


class AssistantMessagePart(BaseModel):
    """Open part envelope; the renderer narrows known part types on the client."""

    model_config = ConfigDict(extra="allow")

    type: str = Field(min_length=1)


class AssistantMessageResponse(BaseModel):
    message_id: str
    thread_id: str
    turn_id: str | None = None
    role: Literal["user", "assistant", "system"]
    content: str
    parts: list[AssistantMessagePart]
    created_at: str | None = None
    legacy_source: str | None = None
    legacy_source_id: str | None = None


class AssistantMessagePage(BaseModel):
    items: list[AssistantMessageResponse]
    next_cursor: str | None

class AssistantThreadCreate(BaseModel):
    model_config = ConfigDict(extra="ignore")

    title: str = Field(default="New conversation", max_length=120)
    scope_mode: Literal["portfolio", "projects"] = "portfolio"
    project_ids: list[str] = Field(default_factory=list, max_length=25)
    web_enabled: bool = False

    @model_validator(mode="after")
    def validate_scope(self):
        if self.scope_mode == "projects" and not self.project_ids:
            raise ValueError("Project scope requires at least one project")
        if self.scope_mode == "portfolio" and self.project_ids:
            raise ValueError("Portfolio scope cannot include project IDs")
        return self


class AssistantThreadPatch(BaseModel):
    model_config = ConfigDict(extra="ignore")

    title: str | None = Field(default=None, max_length=120)
    scope_mode: Literal["portfolio", "projects"] | None = None
    project_ids: list[str] | None = Field(default=None, max_length=25)
    web_enabled: bool | None = None
    archived: bool | None = None



class AssistantTurnRequest(BaseModel):
    model_config = ConfigDict(extra="ignore")

    client_turn_id: str = Field(min_length=1, max_length=160)
    message: str = Field(min_length=1, max_length=20_000)
    command: str | None = Field(default=None, max_length=32)
    intent: AssistantIntent | None = None


class AssistantProjectSearchItem(BaseModel):
    project_id: str
    client_name: str | None = None
    market: str | None = None
    project_status: str | None = None
    current_milestone: str | None = None
