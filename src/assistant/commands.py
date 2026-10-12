"""Deterministic slash-command parsing for AI Workspace turns."""

from __future__ import annotations

from dataclasses import dataclass

UI_COMMANDS = frozenset({"new", "resume", "project", "portfolio", "web", "help"})
AGENT_COMMANDS = frozenset({"status", "compare", "draft"})
ALL_COMMANDS = UI_COMMANDS | AGENT_COMMANDS


@dataclass(frozen=True)
class ParsedCommand:
    name: str
    arguments: str = ""


def parse_slash_command(text: str) -> ParsedCommand | None:
    """Parse a leading slash command without invoking an LLM."""
    stripped = (text or "").strip()
    if not stripped.startswith("/"):
        return None
    token, _, arguments = stripped[1:].partition(" ")
    name = token.strip().lower()
    if not name:
        return None
    return ParsedCommand(name=name, arguments=arguments.strip())
