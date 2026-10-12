"""Verified result contracts for legacy learning mutations."""
from __future__ import annotations

from pydantic import BaseModel


class LearningWriteReceipt(BaseModel):
    """A learning mutation result, successful only after read-after-write verification."""

    success: bool
    verified: bool = False
    operation: str
    record_id: str | None = None
    version: str | None = None
    text: str = ""
    code: str | None = None
