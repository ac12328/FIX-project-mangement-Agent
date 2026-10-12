"""Shared API contracts for project and support web chat."""

from __future__ import annotations

from typing import Literal
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field

MAX_SESSION_MESSAGES = 20


class ChatCapabilities(BaseModel):
    """Execution and persistence behavior for the authenticated chat actor."""

    read_only_execution: bool
    persists_history: bool
    accepts_session_messages: bool
    max_session_messages: int = MAX_SESSION_MESSAGES


class ChatMessage(BaseModel):
    """Stable chat message representation shared by both dashboard agents."""

    model_config = ConfigDict(extra="ignore")

    message_id: str = Field(min_length=1)
    turn_id: str = Field(min_length=1)
    role: Literal["user", "assistant"]
    content: str = Field(min_length=1, max_length=100_000)
    created_at: str | None = None
    persisted: bool


class ChatRequest(BaseModel):
    """Send one message, optionally with an ephemeral read-only session context."""

    model_config = ConfigDict(extra="ignore")

    message: str = Field(min_length=1, max_length=20_000)
    session_messages: list[ChatMessage] = Field(
        default_factory=list,
        max_length=MAX_SESSION_MESSAGES,
    )


class ChatHistoryResponse(BaseModel):
    messages: list[ChatMessage]
    capabilities: ChatCapabilities


class ChatSendResponse(ChatHistoryResponse):
    reply: str
    active_turn_id: str


def chat_capabilities(*, read_only: bool) -> ChatCapabilities:
    """Build the actor-specific capability contract returned by chat routes."""
    return ChatCapabilities(
        read_only_execution=read_only,
        persists_history=not read_only,
        accepts_session_messages=read_only,
    )


def normalize_session_messages(messages: list[ChatMessage]) -> list[ChatMessage]:
    """Treat client-provided session messages as untrusted, ephemeral context."""
    normalized: list[ChatMessage] = []
    for message in messages[-MAX_SESSION_MESSAGES:]:
        normalized.append(
            ChatMessage(
                message_id=message.message_id or f"ephemeral-{uuid4().hex}",
                turn_id=message.turn_id or f"ephemeral-turn-{uuid4().hex}",
                role=message.role,
                content=message.content,
                created_at=message.created_at,
                persisted=False,
            )
        )
    return normalized


def build_ephemeral_turn(
    session_messages: list[ChatMessage],
    *,
    user_message: str,
    reply: str,
    turn_id: str,
) -> list[ChatMessage]:
    """Append a non-persisted user/assistant turn to the supplied session context."""
    messages = normalize_session_messages(session_messages)
    messages.extend(
        [
            ChatMessage(
                message_id=f"ephemeral-{uuid4().hex}",
                turn_id=turn_id,
                role="user",
                content=user_message,
                persisted=False,
            ),
            ChatMessage(
                message_id=f"ephemeral-{uuid4().hex}",
                turn_id=turn_id,
                role="assistant",
                content=reply,
                persisted=False,
            ),
        ]
    )
    return messages
