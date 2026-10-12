"""Typed Telegram inline-button callback data (profile:action[:entity_id])."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class CallbackData:
    profile: str
    action: str
    entity_id: str | None = None
    confirmed: bool = False


def parse_callback_data(data: str) -> CallbackData | None:
    """Parse ``PROFILE:ACTION`` or ``PROFILE:ACTION:ENTITY`` callback strings."""
    parts = data.split(":", 2)
    if len(parts) < 2 or not parts[0] or not parts[1]:
        return None
    profile = parts[0]
    action = parts[1]
    entity_id = parts[2] if len(parts) > 2 else None
    confirmed = False
    if action.startswith("CONFIRMED_"):
        confirmed = True
        action = action[len("CONFIRMED_"):]
    return CallbackData(
        profile=profile,
        action=action,
        entity_id=entity_id,
        confirmed=confirmed,
    )


def format_callback_data(
    profile: str,
    action: str,
    entity_id: str | None = None,
    *,
    confirmed: bool = False,
) -> str:
    """Build a callback string for Telegram inline keyboards."""
    action_token = f"CONFIRMED_{action}" if confirmed else action
    if entity_id is not None and entity_id != "":
        return f"{profile}:{action_token}:{entity_id}"
    return f"{profile}:{action_token}"


def requires_two_click_confirmation(profile: str, action: str) -> bool:
    """Return True when D4 undo confirmation is required before dispatch."""
    return (profile, action) in confirm_required_actions()


def confirm_required_actions() -> frozenset[tuple[str, str]]:
    """Destructive actions that need CONFIRMED_ prefix on second click."""
    return frozenset({("A", "APPROVE")})


def is_generic_cancel(parsed: CallbackData) -> bool:
    """Generic cancel acknowledges only; CU/K route to handlers for real cancel."""
    return parsed.action == "CANCEL" and parsed.profile not in ("CU", "K")