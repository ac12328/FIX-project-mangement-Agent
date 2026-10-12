"""Shared API response models."""

from pydantic import BaseModel


class ActionResult(BaseModel):
    success: bool
    message: str
