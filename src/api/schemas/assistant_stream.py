"""Validated event payloads for the Workspace turn SSE protocol.

StreamingResponse has no response-model validation, so validate each emitted event
at the protocol boundary rather than relying on the normal HTTP schema.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator


class _TurnEventData(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    turn_id: str = Field(min_length=1)


class StartedData(_TurnEventData):
    replayed: bool = False


class ProgressData(_TurnEventData):
    phase: str
    message: str


class TextDeltaData(_TurnEventData):
    delta: str


class PartAddedData(_TurnEventData):
    part: dict[str, Any]

    @field_validator("part")
    @classmethod
    def require_part_type(cls, part: dict[str, Any]) -> dict[str, Any]:
        if not isinstance(part.get("type"), str) or not part["type"]:
            raise ValueError("Assistant part requires a non-empty type")
        if part["type"] == "table":
            columns = part.get("columns")
            rows = part.get("rows")
            if not isinstance(columns, list) or not all(
                isinstance(column, dict)
                and isinstance(column.get("key"), str) and column["key"]
                and isinstance(column.get("label"), str) and column["label"]
                for column in columns
            ):
                raise ValueError("Table part requires columns with non-empty keys and labels")
            if not isinstance(rows, list) or not all(isinstance(row, dict) for row in rows):
                raise ValueError("Table part requires rows of objects")
        return part


class CompletedData(_TurnEventData):
    message_id: str | None = None
    replayed: bool = False


class FailedData(_TurnEventData):
    error: str


_EVENT_DATA_MODELS: dict[str, type[_TurnEventData]] = {
    "turn.started": StartedData,
    "progress": ProgressData,
    "text.delta": TextDeltaData,
    "part.added": PartAddedData,
    "turn.completed": CompletedData,
    "turn.failed": FailedData,
}


def validate_assistant_stream_event(event: str, data: dict[str, Any]) -> dict[str, Any]:
    """Reject unsupported/malformed frames without adding optional absent fields."""
    try:
        model = _EVENT_DATA_MODELS[event]
    except KeyError as exc:
        raise ValueError(f"Unsupported assistant stream event: {event}") from exc
    return model.model_validate(data).model_dump(exclude_unset=True)

